
# MoviePilot V3 后端架构提升与分阶段治理方案

> 文档性质：现状审计、目标约束、迁移路线和 AI 实施手册
> 适用仓库：`MoviePilot`，分支 `v3`
> 审计基线：2026-08-18 当前工作树
> 相关规范：`AGENTS.md`、`docs/rules/05-architecture.md`、`docs/architecture-overview.md`、`docs/refactor/backend-module-refactor-compatibility.md`

## 1. 文档目的

本文件不是另一份目录说明，也不是一次大规模重构设计稿。它解决四个更具体的问题：

1. 区分已经完成的物理目录迁移与仍未解决的职责、依赖和运行时契约问题。
2. 把问题定位到具体模块、类、方法和调用边界，给出可逐批落地的迁移方向。
3. 为其他 AI 提供可以直接执行的任务边界、兼容约束、验证命令和完成标准。
4. 在不破坏 V3 插件生态的前提下，逐步收敛宿主内部结构，而不是用一次性改名制造新的兼容层。

本文同时记录治理方案和当前工作树的实施状态。2026-08-18 已完成本轮"按层职责拆分"的收口批次：阶段 0-7 的边界工作、插件宿主职责拆分、组合根注入和 SDK/Compat 门禁均已落地；仍保留的千行级文件属于同一职责域内的兼容 Facade、厂商协议实现或第三方移植代码，不再作为跨层混合问题处理。每个阶段是否完成必须以本文件的机器基线、聚焦测试、插件兼容扫描和完整测试门禁为准，不能只凭目录已经创建判断。

### 2026-08-18 收口结论

本批次的"全部拆完"指跨层职责和依赖边界完成收敛，不指把所有历史 ABI 类名删除或把每个厂商实现机械切成小文件。当前已验证的关键收口如下：

1. API、Agent、Workflow、Chain 不再直接构造插件/模块 Runtime 管理器；入口通过 `app.application.plugin.runtime.get_plugin_manager()`、`app.application.module.get_module_manager()` 和 `app.application.scheduling.get_scheduler()` 等端口访问，启动层负责实例装配。
2. `ChainBase` 不再静态导入模块调度器，`ModuleInvocationDispatcher` 由启动组合根经 `ChainRuntimeContext.module_dispatcher_factory` 注入。
3. `PluginManager` 的加载、生命周期、注册表、投影、存储、目录、路径、同步、依赖、克隆和文件监控分别由 `app/runtime/extensions/` 各阶段包下的单职责组件承担；旧管理器只保留 V3 ABI 门面和兼容调用顺序。
4. 动态插件 API 使用专用 raw 路由；主程序统一响应信封不进入插件 `get_api()`。前端 `pluginApi` 对非 `Response` envelope 的 payload 原样交付调用方。
5. 旧插件导入仅由 `app/runtime/compat/manifest.py` 精确映射；canonical 模块不复制旧 Manager/Helper/Oper 导出。`app/plugins/` 仍是运行时副本，继续排除在宿主架构扫描之外。
6. 当前机器基线为 746 个宿主 Python 模块、6,024 条内部导入边；数据库边界、Adapter→DB、Runtime→DB、Application→DB 及新增 API/Agent/Chain 目标边均为 0。架构门禁、插件兼容快照和基线脚本均已重新生成。
7. 订阅写入统一归入 `app/application/subscription/write.py`；插件动态路由和文件夹操作统一归入 `app/application/plugin/routes.py`、`folders.py`。重构期间新增且未形成插件 ABI 的 `app/application/subscribe.py`、`app/application/plugins.py` 已直接删除，不进入 compat manifest。

## 2. 范围与明确排除项

### 2.1 纳入范围

- FastAPI 入口、路由、响应封装和动态路由注册。
- `chain` 编排层、`application` 应用能力、`domain` 领域语义。
- `runtime` 进程级基础设施、事件、模块、插件和服务生命周期。
- `adapters` 技术适配与命名外部系统。
- `db/models`、`db/oper`、会话与事务边界。
- `modules` 宿主模块 SPI 及其与应用层的交互。
- Agent、LLM Provider、工具注册和流式 API 的职责边界。
- `sdk` 与 `runtime/compat` 形成的插件公开 ABI。
- 启动、关闭、安全模式、热重载和后台任务的组合关系。

### 2.2 排除项

- **不审计、不迁移 `app/plugins/` 中的代码。**该目录是已安装插件副本，不是后端架构源代码，也不能作为插件兼容性的唯一事实来源。
- 插件兼容基线应读取同工作区独立仓库 `../MoviePilot-Plugins` 的 `plugins.v2/`、`plugins.v3/`，再配合宿主的 SDK、兼容清单和插件管理器契约判断。
- 不把 `app/modules/themoviedb/` 内部第三方或移植代码的局部循环，直接等同于 MoviePilot 自有架构失败。它需要被隔离，但不应优先重写上游库。
- 本轮不主张数据库表结构变更。纯架构批次不得夹带 Alembic 迁移、字段重命名或数据回填。
- 本轮不主张删除 V3 兼容映射。任何删除都应作为显式破坏性变更另行决策。

## 3. 结论摘要

MoviePilot V3 已经完成一轮重要基础工作：原 `app/core`、`app/helper`、`app/utils` 已转为虚拟兼容入口；`foundation`、`domain`、`runtime`、`adapters`、`application`、`chain`、`startup`、`sdk` 的目标方向也已经写入规范；现有架构门禁通过。

以下八类是本轮治理开始时的审计问题清单，不代表 2026-08-18 收口后的未完成项；当前剩余工作以"3.1 当前未完成项"和各阶段收口表为准：

1. **规范比门禁严格（历史基线）。**治理前测试只覆盖部分目标依赖和 SCC，隔离的 TMDB 移植包仍保留上游式局部环；本轮已将宿主自有模块和主要越层边纳入机器基线。
2. **核心运行契约是字符串和约定。**`ChainBase.run_module()` 依赖方法名、签名探测、返回值形态和执行顺序；插件生命周期也依赖一组隐式 `get_*`/`init_*` 方法。它们是实际 ABI，却没有统一契约清单。
3. **编排类和端点承担过多职责。**订阅、搜索、整理、下载、Agent、插件管理、外部市场和服务端客户端均出现千行级文件、百行级方法和多种基础设施混合。
4. **数据库边界没有收口（历史基线）。**治理前 API、Chain、Scheduler、Application 存在 ORM 模型或会话直连；本轮已通过数据端口、Repository/Oper 和组合根注入清零机器基线中的目标边。
5. **组合根仍有泄漏（历史基线）。**治理前存在导入期 app、事件解析器兜底实例化和 Chain 隐式抓取管理器；本轮已改为生命周期/运行时上下文显式装配。
6. **Adapter、Application、Runtime 之间仍有历史职责混合（历史基线）。**外部市场、服务端、插件生命周期和动态路由已拆为端口、适配器、应用用例及运行时组件；未迁出的旧 ABI 实现只保留在正式兼容入口。
7. **插件兼容面大且缺少版本化。**旧导入、SDK、管理器具体类型、动态 API、事件装饰器、模块方法和热重载行为共同构成 ABI；目前主要靠兼容清单和测试样例保护。
8. **治理缺少可量化收敛目标（历史基线）。**本轮已补充模块/导入边/SCC、事件、插件 hook、SDK/Compat 和启动矩阵快照；后续变更必须更新机器基线并说明是否属于同一职责域内的实现细化。

### 3.1 当前未完成项

按"全部拆完"的边界，宿主跨层职责已经收口；当前只剩三类不宜继续机械拆分的工作：

1. `app/runtime/extensions/plugin_manager.py` 与 `app/adapters/external/market.py` 仍保留正式 V3 ABI 的兼容 Facade/算法实现，继续迁移必须按私有方法命中数据和行为快照逐步进行，不能复制旧类或删除旧路径。
2. `app/modules/themoviedb/` 等第三方移植代码的局部 SCC 属于上游实现隔离项，不纳入宿主跨层拆分目标。
3. 新增业务能力仍需遵守端口、组合根、单词文件命名和插件 raw 响应约束；这些是持续门禁，不是本轮遗留拆分任务。

治理顺序必须是：**先冻结行为契约和补门禁，再拆环和依赖，再拆职责，最后才讨论缩减兼容面。**

## 4. 审计方法与当前基线

### 4.1 方法

1. **静态导入分析**：用 `tests/test_architecture_dependencies.py` 扫描 `app/*` 下的第一层导入。
2. **模块规模和 SCC 量化**：用 `scripts/architecture/baseline.py --check` 生成模块数、导入边、SCC 和强制性公开 API。
3. **插件兼容扫描**：在独立仓 `../MoviePilot-Plugins` 上运行 `tests/ci/test_v3_contract.py` 和 `test_plugin_release_gate.py`。
4. **全量测试**：`./.venv/bin/python tests/run.py` 跑 pytest、pytest-asyncio、pytest-cov 套件。

所有门禁结果和基线都是代码内生成的，而不是人工维护的清单。

### 4.2 测试与检查清单

```text
./.venv/bin/python -m pytest tests/test_architecture_dependencies.py -q
28 passed
```

这只能证明当前代码符合现有门禁，不能证明符合本文件提出的更完整目标。

### 4.3 模块规模

排除 `app/plugins/` 后，当前静态扫描得到 746 个 Python 模块、6,024 条内部导入边。主要一级目录规模如下（代码行数包含注释和空行，用于趋势比较而非质量评分）：

| 一级目录 | 约代码行数 | Python 文件数 | 判断 |
| --- | ---: | ---: | --- |
| `app/modules` | 67,526 | 151 | 体量最大，具体平台协议和第三方移植代码留在模块族内部 |
| `app/agent` | 40,510 | 141 | Provider、工具、编排和策略各自有子域；后续只做域内优化 |
| `app/chain` | 29,703 | 36 | 大型用例链保留历史行为，跨层依赖已经由端口收口 |
| `app/api` | 16,882 | 44 | 端点保留传输映射和协议特例，业务/持久化经 Application 端口完成 |
| `app/application` | 19,273 | 81 | 应用用例、端口和兼容门面集中，禁止反向依赖 Runtime 实现 |
| `app/runtime` | 14,025 | 49 | 模块/插件/事件生命周期和限流已拆出，宿主整体生命周期仍集中 |
| `app/adapters` | 13,014 | 37 | HTTP、缓存、系统调用、外部服务已分出，市场/包管理的端口隔离仍在进行 |
| `app/db` | 8,238 | 50 | 模型兼容层和根入口已收敛，剩余局部环属于后续迭代范畴 |

### 4.4 核心实现根（最高层 import）

| 分类 | 模块 | 入度 | 出度 | 说明 |
| --- | --- | --- | --- | --- |
| 入口 | `app/api` | 0 | 5 | FastAPI、响应、端点、工具、中间件 |
| | `app/agent` | 0 | 3 | 编排、运行时、工具、LLM、记忆 |
| | `app/monitor` | 0 | 2 | 监控、分发 |
| | `app/workflow` | 0 | 1 | 工作流引擎 |
| | `app/cli` | 0 | 2 | 命令行 |
| 核心 | `app/chain` | 1 | 4 | Module 分发、Application、Domain、Runtime |
| | `app/application` | 1 | 4 | Module、Domain、Runtime、Adapter |
| | `app/modules` | 1 | 2 | Domain、Adapter |
| 建筑 | `app/domain` | 0 | 2 | Schema、Foundation |
| | `app/runtime` | 0 | 2 | Schema、Foundation |
| | `app/adapters` | 0 | 2 | Domain、Foundation |
| | `app/foundation` | 0 | 0 | — |

### 4.5 禁止目标边

以下依赖在门禁中被强制为零，不得新增：

- `app.api -> app.db.models.*` 与 `app.db.session`
- `app.api -> app.runtime.extensions.plugin_manager`、`module_manager`、`scheduler`
- `app.adapters -> app.application`、`app.chain`、`app.db`
- `app.runtime -> app.db` （除 `app.runtime.extensions` 通过 `Oper` 端口）
- `app.foundation -> 任何 app.*`
- 任何形成 SCC 的模块级循环依赖

## 5. 问题分类与迁移方案

### 5.1 核心实现根设计缺失

**现象**：`app/api`、`app/agent`、`app/workflow`、`app/cli` 直接创建或获取 `ModuleManager`、`PluginManager`、`Scheduler` 等全局单例；`app/chain/*` 各文件在类初始化时自行抓取这些管理器。

**根因**：生命周期管理缺少统一入口，Manager 在导入时创建单例而非在组合根显式装配。

**目标**：消除直接依赖，通过组合根注入和运行时上下文透传；`app/startup/lifecycle.py` 成为唯一的实例装配点。

**迁移时机**：已完成 ✓。

1. 启动组合根 `startup/lifecycle.py` 构建 `ChainRuntimeContext` 并绑定到 FastAPI lifespan。
2. Chain、Application、Agent 各子域通过 `@contextvar` 或依赖注入获取注入的 `module_dispatcher`、`plugin_manager` 等。
3. API 端点经 `app/api/deps.py` 注入，不再从 `SystemConfig` 抓取管理器。
4. 后台任务（调度器、监控、工作流）通过类构造时传入的参数获取管理器，不自行单例化。

**验证**：`tests/test_architecture_dependencies.py` 中 `test_no_api_import_manager`、`test_no_scheduler_import_manager` 两项测试。

### 5.2 Module 分发协议缺少类型

**现象**：`run_module(method_name, **kwargs)` 的方法名、参数、返回值完全依赖约定和文本文档；没有机制保证插件实现的签名和宿主使用之间的一致性。

**根因**：最初是为了支持任意 Module 实现而采取鸭子类型；现在已经有了稳定的 SPI，却缺少正式契约。

**目标**：为每个分发族（下载器、媒体服务器、消息渠道等）生成 `@dataclass` 或 `Protocol` 契约，装配时验证签名；插件的方法签名与宿主期望不符时，启动失败或给出警告。

**迁移时机**：已完成 ✓。

1. `app/runtime/extensions/module/protocols.py` 定义了每个 Module 族的方法和参数类型。
2. `ModuleInvocationDispatcher` 在分发前验证方法存在、参数对齐。
3. 插件若实现了不匹配的方法签名，装配时由 `validation_module_interface` 捕获。

**验证**：`tests/test_module_manager_capability_adapter.py`、`tests/test_module_protocol_validation.py`。

### 5.3 Plugin 生命周期职责混合

**现象**：`PluginManager` 达到 ~1000 行，混合了文件 I/O、导入、实例化、钩子、事件、同步、更新检查、依赖解析、SDK 生成和热重载。同时，插件的 `init_plugin()` / `get_*()` 钩子是约定，不是正式 ABI。

**根因**：最初 `PluginManager` 包一切，后来逐步外包但没有清除旧代码；Plugin ABI 是隐式的，仅依赖方法名搜索。

**目标**：拆解为单职责组件，Plugin ABI 形成正式白名单和版本门禁。

**迁移时机**：已完成 ✓。

- `app/runtime/extensions/plugin/` 下各模块各自负责一个职责：
  - `loader.py`：导入、编译、动态重载。
  - `lifecycle.py`：初始化、停止、配置变更重载。
  - `registry.py`：版本管理、关键字索引、查询 API。
  - `projection.py`：插件能力投影、Schema 和约定方法的快照。
  - `storage.py`：插件数据持久化、配置存取。
  - `dependency.py`：依赖解析、版本检查。
  - `watcher.py`：文件夹监控和热重载触发。
  - `directory.py`：目录规范和权限隔离。
  - `sync.py`：市场同步、版本更新检查。
- `PluginManager` 保留 V3 ABI 门面和兼容调用顺序，内部代理到各组件；旧导入如 `from app.core import PluginManager` 仍映射到此。

**验证**：`tests/test_plugin_component_isolation.py` 中的 loader / lifecycle / projection / storage 各自测试；插件兼容扫描 `test_plugin_release_gate.py`。

### 5.4 Plugin 与 Application 的边界

**现象**：动态插件 API 路由 `/api/v1/plugin/{plugin_id}/*` 中，`get_api()` 钩子返回 FastAPI 的 `APIRouter`，其中可能直接写业务逻辑、持久化或构造 Event；Plugin 按需抓全局 DB 引擎、事件管理器。

**根因**：插件是宿主的一等扩展，但调用边界模糊；响应信封在宿主 router 中加，还是让插件自己处理？

**目标**：动态插件 API 使用专用 raw 路由；宿主响应信封不进入插件 `get_api()`，前端 `pluginApi()` 对非 `Response` envelope 的返回值原样交付。

**迁移时机**：已完成 ✓。

1. `/api/v1/plugin/{plugin_id}/*` 路由注册时由 `app/application/plugin/routes.py` 添加前置拦截，处理通用异常和流式响应。
2. 插件的 `get_api()` 返回的 router 中的 handler 必须返回被宿主允许的原始类型（dict、list、str、None、Response）；如返回其他，拦截和序列化失败时返回 500。
3. 前端收到 payload 时判断是否含有 `code`/`data`/`message`；含有时当作 envelope 解析，否则直接使用。

**验证**：`tests/test_plugin_api_response_envelope.py`、`tests/test_dynamic_api_contract.py`。

### 5.5 旧插件兼容映射范围与版本化

**现象**：旧导入如 `from app.core.plugin import PluginManager` 被 manifest 映射到 `from app.runtime.extensions.plugin_manager import PluginManager`；但旧 import 路径与新位置不对等，导致导入路径变多、兼容债务增长。

**根因**：迁移过程中为了向后兼容而制造的虚拟层。

**目标**：兼容清单冻结为精确白名单，不再为新增导出创建兼容别名；原有映射由 `manifest.py` 统一编译为 `sys.modules` 劫持，保证"删除就完全删除"。

**迁移时机**：已完成 ✓。

1. `app/runtime/compat/manifest.py` 声明并维护所有允许的旧导入路径及其映射目标。
2. 任何新增宿主导出一律在 canonical 位置引入，不在 manifest 增加新映射；旧插件若需要新增功能，应改为新导入路径。
3. 兼容清单由 `scripts/sdk/exports.py --check` 自动验证，`--write` 更新需求表但不自动增加 SDK 导出；SDK 导出由人工决策。
4. `app/plugins/` 仍是运行时副本，继续排除在宿主架构扫描之外。

**验证**：`tests/test_compat_manifest.py`、插件市场适配和 release gate。

### 5.6 Plugin 与 SDK 的版本化与冻结

**现象**：插件导入路径分散（app.core / app.helper / app.sdk / 直接 app.* 导入），SDK 的公开 API 列表与实际导出不对齐，插件更新时易触发隐式依赖破坏。

**根因**：SDK 是逐步演进的，没有明确版本边界；很多插件代码直接 import 宿主内部包，破坏了分层隔离。

**目标**：SDK 冻结为正式版本化产品，旧插件导入仅通过 compat manifest，新插件只能使用 SDK；每个 SDK 版本号对应一批插件兼容的宿主 API。

**迁移时机**：已完成 ✓（compat / SDK 部分）；后续版本升级时遵守。

1. `app/sdk/` 是新插件的稳定导入面，包含网络、缓存、日志、浏览器等通用能力。
2. `app/runtime/compat/manifest.py` 列出所有宿主允许的旧导入路径及其 SDK 对等物。
3. 插件兼容检查 `test_plugin_release_gate.py` 验证：新插件导入仅在 manifest 或 SDK 中，旧插件使用的导入路径必须在 manifest 中。
4. SDK 的公开导出由 `scripts/sdk/exports.py` 生成白名单，一旦冻结即不可逆。

**验证**：插件仓 `test_v3_contract.py`、独立 `test_plugin_release_gate.py`、市场上传时的导入扫描。

### 5.7 Application 层设计

**现象**：原 `app/application/` 只是聚合，`subscribe.py`、`search.py`、`download.py` 等混合用例编排、持久化、业务规则；没有端口隔离；Application → Agent/Runtime/Adapter 的依赖方向有反向。

**根因**：Application 本应是单向依赖的"业务语义"层，但逐步积累了"告诉谁做什么"的编排职责。

**目标**：用单词文件（`subscribe.py`、`search.py` 等）划分用例，Application 内不形成跨用例的二级聚合；端口模式明确化（谁提供什么、谁消费什么）；禁止 Application → Agent 实现、Runtime 实现细节的依赖。

**迁移时机**：已完成 ✓。

| 单词模块 | 职责 | 禁止 |
| --- | --- | --- |
| `subscription/` | 订阅写入与进度跟踪 | 反向依赖 Plugin/Module 实现；直接访问 ORM Session；复制 Chain 编排逻辑 |
| `search/` | 搜索状态和缓存 | 直接调用 Module 分发；独立编排 Chain 的逻辑 |
| `download/` | 下载任务查询/控制 | 直接访问下载器实现；编排下载流程（应由 Chain 完成） |
| `plugin/` | 动态路由、安装命令、文件夹操作 | 构造 PluginManager；直接读写插件存储 |
| `plugin/runtime.py` | 插件 Manager 端口 | *(注：此为兼容 Facade，内容由 `runtime/extensions/plugin_manager.py` 代理)* |
| `messaging/` | 消息渲染、交互会话、Agent 网桥 | 直接处理底层协议；反向依赖 Agent 实现 |
| `security/` | 认证、授权、Cookie、SSRF | 直接访问 HTTP 实现；自行序列化 Token |
| 其他用例 | 媒体、站点、管理、系统 | — |

`app/application/` 的职责已由 `Chain` 独占（用例编排）与 `Application` 各模块（共享能力、端口、规范实现）完成分工；不再允许新增跨模块文件。

**验证**：`test_application_boundaries.py`、各用例的集成测试。

### 5.8 Database 边界与 Oper 纵深

**现象**：`app/api` 端点中仍存在直接写 `sqlalchemy.select()` 或调用 `session.query()` 的代码；少数 Module 实现也直接构造 ORM 模型对象或拼接查询；`app/db/models/*.py` 中的某些模型含有查询方法（如 `Media.query_by_tmdb_id()`），导致数据访问逻辑分散。

**根因**：Oper 模式推出不够完整；部分早期代码未迁移；没有强制机制。

**目标**：所有数据访问一律经由 `app/db/oper/*.py`；模型文件只含数据定义和 lifecycle hook（`before_insert` / `before_update` / `before_delete`），不含查询或持久化逻辑。

**迁移时机**：已完成 ✓（检查通过）。

1. 所有数据访问由 `Oper` 类统一完成；Oper 返回持久化值（ORM 模型实例或标量）。
2. Chain / Application 接收 `MediaInfo` / `MetaBase` 等业务对象；与 ORM 模型的转换由 Application 层完成。
3. API 端点接收请求、调用 Application 或 Chain 获取业务数据、转为 Pydantic Schema、返回响应；中间不含 ORM。
4. Module 实现不持有数据库引擎或会话；必要的数据查询由宿主端口注入或通过参数传递。

**验证**：`test_architecture_dependencies.py` 中的 `test_no_direct_orm_access` 等；且后续新增 API、Chain 目标边仍为零。

### 5.9 Runtime 与 Adapter 的倒向依赖

**现象**：某些 Plugin Manager 中调用 Oper（`from app.db.oper.plugin_install import ...`）；Service Registry 实现中直接读 SystemConfig；Adapter 中的包管理器创建 Manager 单例。

**根因**：这些职责本应在 startup 阶段完成，但因为生命周期管理混乱而被分散到各处。

**目标**：Runtime 与 Adapter 禁止单向依赖 Application、Chain、DB；需要的数据与管理器由 startup 组合根通过构造参数或 DI 注入，不自行创建。

**迁移时机**：已完成 ✓。

1. `app/runtime/extensions/plugin/lifecycle.py` 初始化前由 startup 注入 `PluginStoragePort`、`PluginRepositoryPort` 等数据端口。
2. `ModuleInvocationDispatcher` 作为纯分发器，无状态；其所需的 Module registry 由运行时上下文透传。
3. `app/adapters/` 中的技术适配器（HTTP、缓存、系统调用等）只负责工程，不创建业务对象或调用业务逻辑。
4. Service Registry（若有）的发现、注册由 startup 驱动，不由 Adapter 自行触发。

**验证**：`test_no_adapter_to_db` 等目标边检查；startup lifecycle 测试。

### 5.10 Event 运行时与约定

**现象**：事件装饰器 `@eventmanager.register(EventType.X)` 的执行顺序、错误处理、异步/同步混合等细节缺乏约定；插件可以注册事件处理器，但没有隔离和限流。

**根因**：事件总线是跨切面的基础设施，但约定不清。

**目标**：事件类型集中定义（`schemas.types.EventType`），处理器签名标准化（`async def handler(event: Event) -> None`），错误隔离（一个处理器异常不影响其他），限流和超时配置。

**迁移时机**：已完成 ✓。

1. `EventManager` 在 `app/runtime/events.py` 中管理全局事件总线。
2. 所有事件类型由 `EventType` 枚举集中定义，插件扩展事件时只能增加自定义类型，不能删除标准事件。
3. 处理器注册时由装饰器验证签名；异步/同步处理器分别入队，宿主负责调度和错误捕获。
4. 插件处理器执行超时、异常不传播；日志记录但允许其他处理器继续执行。

**验证**：`test_event_isolation.py`、插件事件处理测试。

## 6. 逐阶段迁移清单

### 6.1 阶段 0：目标定义与基线建立

**描述**：冻结架构规范、生成当前代码基线、建立检查门禁。

**完成标志**：

```text
./.venv/bin/python tests/test_architecture_dependencies.py -q
28 passed
```

- `docs/architecture-overview.md` 与 `docs/rules/05-architecture.md` 已定版。
- `scripts/architecture/baseline.py --check` 通过，基线文件已生成。
- 架构门禁已纳入 CI/CD 流程。

**所有者**：架构师 + 测试框架负责人。

**工作量估算**：~ 40 h（包括 baseline 脚本开发、学习曲线、多次基线重新生成）。

**已完成**：✓

---

### 6.2 阶段 1：组合根与生命周期隔离

**目标**：Manager（ModuleManager、PluginManager、Scheduler 等）的生命周期从导入期延迟到启动期；入口（API、Agent、CLI 等）不直接构造或全局获取。

**关键文件**：
- `app/startup/lifecycle.py`：构建 ChainRuntimeContext，通过 FastAPI lifespan 注入。
- `app/api/deps.py`：Depends() 工厂获取注入的 context。
- `app/chain/__init__.py`：Chain 基类改为接收 context 或通过 contextvar 获取。

**验证**：
```text
./.venv/bin/python -m pytest tests/test_architecture_dependencies.py::test_no_api_import_manager -q
1 passed
```

**完成状态**：已完成 ✓

---

### 6.3 阶段 2：Module 分发协议与验证

**目标**：定义 Module SPI，装配时检验插件实现与宿主期望的一致性。

**关键文件**：
- `app/runtime/extensions/module/protocols.py`：Module 族的 Protocol 定义。
- `app/runtime/extensions/projection/dispatcher.py`：装配时的签名验证。
- 各 Module 实现：补充类型注解和 docstring。

**验证**：
```text
./.venv/bin/python -m pytest tests/test_module_manager_capability_adapter.py -q
8 passed
```

**完成状态**：已完成 ✓

---

### 6.4 阶段 3：Plugin 生命周期拆解

**目标**：PluginManager 由单一对象拆为单职责组件（loader、lifecycle、registry、projection、storage、dependency、watcher、directory、sync）。

**关键文件**：
- `app/runtime/extensions/plugin/loader.py`
- `app/runtime/extensions/plugin/lifecycle.py`
- `app/runtime/extensions/registry/plugin.py`
- `app/runtime/extensions/projection/plugin.py`
- `app/runtime/extensions/lifecycle/storage.py`
- `app/runtime/extensions/plugin/dependency.py`
- `app/runtime/extensions/plugin/watcher.py`
- `app/runtime/extensions/plugin/directory.py`
- `app/runtime/extensions/plugin/sync.py`
- `app/runtime/extensions/plugin_manager.py`：兼容 Facade，内部代理。

**验证**：
```text
./.venv/bin/python -m pytest tests/test_plugin_component_isolation.py -q
20 passed
```

**完成状态**：已完成 ✓

---

### 6.5 阶段 4：Plugin ABI 冻结与版本化

**目标**：Plugin 的公开钩子、API 返回值形式、依赖宿主导出的范围形成白名单；插件兼容门禁建立。

**关键文件**：
- `app/runtime/compat/manifest.py`：旧导入映射白名单。
- `scripts/sdk/exports.py`：SDK 导出清单与验证。
- `tests/ci/test_v3_contract.py`：插件 hook、API、导入检查。
- `tests/ci/test_plugin_release_gate.py`：插件市场上传前的门禁。

**验证**：
```text
../MoviePilot-Plugins/.venv/bin/python -m pytest tests/ci/test_v3_contract.py tests/ci/test_plugin_release_gate.py -q
16 passed
```

**完成状态**：已完成 ✓

---

### 6.6 阶段 5：Database 边界清零

**目标**：移除所有直接 ORM 访问（select / query / session），数据交互一律经 Oper。

**检查清单**：
- `app/api/endpoints/*.py`：移除 sqlalchemy 导入；通过 Application 或 Chain 获取数据。
- `app/modules/*/*.py`：移除 ORM Session 使用；若需要数据，由端口提供。
- `app/db/models/*.py`：仅含模型定义和 lifecycle hook，无查询方法。

**验证**：
```text
./.venv/bin/python -m pytest tests/test_architecture_dependencies.py::test_no_api_direct_orm_access -q
1 passed
```

**完成状态**：已完成 ✓

---

### 6.7 阶段 6：Application 端口与边界定型

**目标**：Application 中各单词文件（subscription、search、download、plugin、messaging 等）职责清晰，禁止反向依赖、跨模块耦合。

**关键更改**：
- `app/application/subscription/write.py`：订阅写入端口。
- `app/application/plugin/routes.py`、`folders.py`、`runtime.py`：插件管理端口。
- `app/application/messaging/`：消息路由、Agent 网桥、交互会话。

**验证**：
```text
./.venv/bin/python -m pytest tests/test_application_boundaries.py -q
15 passed
```

**完成状态**：已完成 ✓

---

### 6.8 阶段 7：Runtime & Adapter 依赖反向清零

**目标**：Runtime 与 Adapter 禁止依赖 Application、Chain、DB；所需功能由组合根注入。

**关键更改**：
- `ModuleInvocationDispatcher`：纯分发，无状态；registry 由运行时上下文透传。
- `PluginManager` 及其组件：初始化时由 startup 注入所有依赖。
- Adapter 中的包管理器、Service Registry：禁止自行创建 Manager 或 Oper。

**验证**：
```text
./.venv/bin/python -m pytest tests/test_no_runtime_to_db tests/test_no_adapter_to_application -q
8 passed
```

**完成状态**：已完成 ✓

---

### 6.9 阶段 8-N：后续优化

待本轮检查通过且插件兼容性确认后再启动下一个治理周期。

#### 6.9.1 `app/runtime` 目录重排：判据与已划定的边界

目录归属由 `docs/rules/05-architecture.md` 的判据 D 决定：目录存在的理由是
「什么时候跑 + 谁能 import」不同，不是主题词相同。按 D1–D4 顺序提问，第一个命中即定位。

已落地：宿主端口槽归入 `app/runtime/hostports/`（D2）；`debounce.py` 归入
`app/runtime/compat/`（D1，宿主零调用方，只由 `app.utils.debounce` 别名吊命）；
扩展契约归入 `app/runtime/extensions/contract/`（D1，符号已随 SDK 交到扩展作者手里）；
扩展生命周期的四个时刻各自成目录：登记期 `admission/`、持有态 `registry/`、
查询期 `projection/`、发现加载 `lifecycle/`。`plugin/` 与 `module/` 两个子目录随之消失——
那层边界的两条候选规则都有反例：按「装插件专属的」分，命令与过滤规则注册表在上层而
插件注册表在下层；按「装内部实现」分，上层的契约与配置模式纯内部，下层却有五个文件
被 `app/startup` 直接 import。

十二个声明族的校验器同批去掉 `_capabilities` 后缀：文件里十七个公开函数有十五个是
`*_declaration_violation()`，它们是校验器不是能力表，后缀指的方向与内容相反。目录已经
说明这是登记期的判定，文件名不再重复它——存储声明的登记校验是 `admission/storage.py`，
持有它的注册表是 `registry/storage.py`。

`contract/` 与 `compat/` 同由 D1 admit，靠「宿主自己还走不走这条路」区分：宿主把所有
调用方都指向它的，是仍在生效的契约；宿主一处都不再调用、只有已发布插件还 import 的，
是兼容面。

**`plugin_manager.py` 不在本轮搬迁范围内。** 它虽然同样命中判据，但迁移成本明显
高于收益，且成本不在自身而在三处外部硬编码：

- `scripts/sdk/exports.py` 的 `HOST_INTERNAL_EXPORTS` 有 5 个 `plugin_manager.*`
  全名串；漏改会让这 5 个宿主内部符号变成 SDK 必需导出，且不会报错，是静默降级。
- `tests/test_plugin_sdk.py` 按 `__module__` 断言符号归属。
- `tests/test_plugin_local_sync.py` 有 14 处 monkeypatch 目标字符串。

三者都是字符串匹配，改错时测试仍可能是绿的，因此该模块只在能同批收紧上述硬编码时
才动，不允许顺手搬。`module_manager.py` 同理。两者都属发现加载期，`lifecycle/` 已建，
待上述硬编码可同批收紧时归位。

**`service_config_validation.py` 不与 `service_config.py` 合并。** 两者确实共用同一套
判定，但合并会成环：`registry/service_instance.py` 静态 import `service_config` 的扇出
函数，而校验一条配置合不合它那个类型的契约必须查服务实例登记表。合并后
`service_config` 反向 import `registry/service_instance`，实测即报
`partially initialized module` 的循环导入。「写得进就用得起来」的保证不靠同处一个文件
维持——两条路径都调用 `contract/config_schema.py` 的同一个判定函数，保证落在那里。
该文件按判据归 `admission/service_config.py`：它判定的是一次写入准不准入，是登记期。

`plugin/method_table.py` 合并进 `admission/module.py`：模块声明的全部契约就是这张方法表，
媒体数据源声明携带的实现表形状与要求完全相同，判定收在一处，两个校验器共用一份规则。

判据 D 在两个文件上判不出唯一归属，按「取用形状与本目录的既有规则是否一致」落定：
`lifecycle/storage.py` 与 `lifecycle/system.py` 是组合根注册、由插件管理器解析的端口，
形状上命中 D2，但 `hostports/` 的规则是「一个协议加一个模块级 `HostPort` 实例，由
`app/startup/hostport_initializer.py` 一处注入」，这两个都不满足——它们是具体类加模块级
全局，由 `app/startup/plugins_initializer.py` 注入，且只在插件的装载与卸载路径上取用，
故归发现加载期。

`tests/test_architecture_dependencies.py` 的 `PLUGIN_COMPONENT_ROOTS` 用 `rglob`
扫描一组硬编码目录。目录一旦改名或搬走，`rglob` 扫空、断言列表为空、测试转绿，
保护随之消失且无任何提示。改动这些目录时必须同批更新常量，并用一处故意违规
验证门禁仍会变红。

覆盖面随目录重排而扩大：`app/runtime/extensions/plugin/` 里的文件被拆进多个阶段目录，
这些目录同时收纳了原先不在门禁范围内的文件，因此新 root 列表覆盖的文件多于旧列表。
`contract/instance.py` 的 `__all__` 就是这样浮出来的——门禁禁止组件模块自建导出清单，
该模块的全部符号本就在同文件定义，删掉清单不改变任何导入行为。

##### 6.9.1.1 `app/runtime/extensions/` 顶层残留文件的归属

`extensions/` 顶层八个文件逐个按判据 D 判定，四个落位、四个留在顶层：

| 文件 | 命中 | 去向 |
| --- | --- | --- |
| `host_module_adapter.py` | D3 发现加载（兼查询期，平局取最早） | `lifecycle/host_module_adapter.py` |
| `managed_resource_adapter.py` | D3 发现加载 | `lifecycle/managed_resource_adapter.py` |
| `paths.py` | D3 发现加载 | `lifecycle/paths.py` |
| `service_instance_requirement.py` | D3 登记期（兼查询期，平局取最早） | `admission/service_instance_requirement.py` |
| `module_manager.py` | D3 发现加载 | 顶层，硬编码未收紧前不搬 |
| `plugin_manager.py` | D3 发现加载 | 顶层，硬编码未收紧前不搬 |
| `service_config.py` | 判不出唯一归属 | 顶层，四个时刻共用的宿主内部底座 |
| `service_registry.py` | D1，但 `contract/` 不收 | 顶层 |

`host_module_adapter.py` 的主体是 manifest 发现、清单契约校验、激活判定与
`HostModuleAdapter` 的 materialize/start/stop，全在登记之前；`HostModuleExtension`
与 `HostModuleProviderSource` 属查询期，按「两个时刻同时认领时归最早的那个」随文件
一并落在 `lifecycle/`。两个适配器文件里的 `Path(__file__).resolve().parents[2]` 随目录
深度改为 `parents[3]`，否则模块与托管资源的发现根会指偏一级、扫不到任何 manifest。

`paths.py` 命中 D1——已发布插件的 `from app.plugins import plugin_instance_path` 经
`SYMBOL_ALIASES` 落到它——但 `contract/` 的规则是「符号随 SDK 交到扩展作者手里」，而
`plugin_instance_path` 不在 SDK 任何一张清单里，插件的 canonical 取用路径是
`_PluginBase.get_data_path()`。`contract/` 另有一条事实规则：整包不 import 任何
`app.runtime`，只依赖 `app.foundation` 与 `app.schemas`；`paths.py` 要读 `settings`、
写文件系统、打日志。它与 `lifecycle/layout.py` 是同一件事的两面——后者是插件源码目录的
版本化布局与存量迁移，前者是插件持久化目录的实例化布局与存量迁移，故归发现加载期。

`service_instance_requirement.py` 的声明形状判定在登记期、候选列举与调用目标裁决在
查询期，平局取最早归 `admission/`。它的 `__all__` 按门禁删除：清单里的符号全部在同文件
定义，且该模块不是任何兼容别名的目标，`scripts/sdk/exports.py` 不会对它调用
`public_surface()`，删清单不改变 SDK 必需导出（`--check` 实测无差异）。

**`service_config.py` 留在顶层。** 五个子包的规则没有一个收得下它：`contract/` 要求
每个公开符号都是交出去的契约，而它二十余个公开符号里只有 `ServiceConfigHelper` 到得了
SDK，其余是宿主内部实现、可以随时改；`admission/` 要求是登记那一刻的判定，而写入准入
判定已经拆成 `admission/service_config.py`；`registry/` 只放登记结果，`_SERVICE_CONFIGS`
是常量表；`projection/` 只读登记态，它读的是用户配置；`lifecycle/` 要求在发现装载卸载
那一刻执行，而它在四个时刻都被读到、专属于哪一个都不成立。真正的理由是分层：
`admission/`、`registry/`、`projection/` 与 `lifecycle/` 都 import 它，它一个都不 import，
位置在四个时刻之下。把共用底座塞进其中一个时刻目录，等于让另外三个反向依赖某个兄弟包
的内部。移出 `app/runtime/extensions/` 平铺到 `app/runtime/` 同样不行：依赖矩阵的
`app.adapters ↛ app.runtime.extensions` 是前缀规则，移出即让该模块失去这条覆盖。

**`service_registry.py` 留在顶层。** 它命中 D1 且与 `contract/` 的规则相符——
`app.helper.service` 的 ModuleAlias 直指本模块，`ServiceBaseHelper` 与
`ServiceConfigHelper` 两个公开符号都是 SDK 导出。落不进去的原因有二，都是硬的：

- 门禁 `test_plugin_components_do_not_reexport_legacy_abi_names` 禁止组件根下的模块
  自建 `__all__`，而本模块的 `__all__` 是载荷。`scripts/sdk/exports.py` 的
  `public_surface()` 优先读 `__all__`，读不到才回落到「本模块定义的类与函数」；
  `ServiceConfigHelper` 是从 `service_config` import 进来的，回落路径取不到它。实测
  删掉 `__all__` 后 `scripts/sdk/exports.py --check` 报 `app.sdk.services` 的必需导出
  少了 `ServiceConfigHelper`——已发布插件的 `from app.helper.service import
  ServiceConfigHelper` 从此不再被 SDK 清单门禁保护，且删清单本身不会报错。
- 它静态 import `module_manager`。落进 `contract/` 会让冻结声明面反向依赖管理器，
  而 `contract/` 现在对 `app.runtime` 零依赖。

五个子包全在 `PLUGIN_COMPONENT_ROOTS` 里，因此这条 `__all__` 禁令对 `contract/`、
`admission/`、`registry/`、`projection/`、`lifecycle/` 一视同仁：本模块搬去哪个子包都撞同
一道门禁。

本批未改 `PLUGIN_COMPONENT_ROOTS` 常量：四个文件都是搬进既有 root，root 列表本身不变。
覆盖面照例扩大，按 §6.9.1 的要求做了变异验证——在 `lifecycle/paths.py` 与
`admission/service_instance_requirement.py` 各注入一个 `__all__`、在
`lifecycle/host_module_adapter.py` 注入一个 `PluginManager` 类，门禁如期报出这三条，
删除注入后恢复绿。`HOST_INTERNAL_EXPORTS` 的 5 个 `plugin_manager.*` 串本批不受影响，
`scripts/sdk/exports.py --check` 全程无差异。

## 7. 职责模型

### 7.1 Package Ownership Matrix

| 包 | 所有者 | 维护周期 | 是否可删除 | 版本化需求 |
| --- | --- | --- | --- | --- |
| `foundation` | 架构 | ~ 年 1 次 | 否 | 高（任何变更破坏整体） |
| `domain` | 业务逻辑 | ~ 月 1-2 次 | 否 | 高（Plugin ABI 依赖） |
| `runtime` | 基础设施 | ~ 月 1-2 次 | 否 | 高 |
| `adapters` | 技术适配 | ~ 月 1 次 | 按适配器 | 中 |
| `chain` | 业务编排 | ~ 周 1-2 次 | 否 | 中 |
| `application` | 能力聚合 | ~ 周 2-3 次 | 各单词模块 | 中 |
| `modules` | 宿主模块 | ~ 周 1-2 次 | 按模块 | 低（各模块独立） |
| `db` | 数据持久化 | ~ 月 1 次 | 否 | 中 |
| `schemas` | 数据模型 | ~ 月 1-2 次 | 否 | 中 |
| `api` | REST 端点 | ~ 周 1-2 次 | 按端点 | 低 |
| `agent` | AI 编排 | ~ 周 1-3 次 | 否 | 中（工具/Provider 签名） |
| `sdk` | 插件接口 | ~ 月 1 次 | 否 | 高（冻结） |
| `plugins` | 已安装插件副本 | ~ 用户定义 | 是 | — |

### 7.2 版本化与 Changelog

新增业务功能（如订阅、搜索）、规范变更（如新的 Module SPI）、Plugin ABI 变更（如移除宿主导出）时：

1. 更新 `CHANGELOG.md`：列出 breaking change、新增能力、已弃用项。
2. Plugin 兼容性检查：扫描已发布插件，若使用了 breaking change，必须升级插件版本或提供过渡期兼容层。
3. 更新 SDK 或 compat manifest：新增导出或旧导入映射。

## 8. 问题诊断与反馈

如发现 merge conflict / 基线不通过 / 新增违反目标边的代码，遵循以下步骤：

1. **快速诊断**：运行 `tests/test_architecture_dependencies.py -vv` 看哪条规则被违反。
2. **定位代码**：grep 或 `scripts/architecture/baseline.py --diff` 找出违反的导入。
3. **评估影响**：是否与已完成的迁移冲突？是否涉及新业务（不在本轮范围）？
4. **上报与修复**：
   - 若影响本轮规范，联系架构师决策是否需要基线调整或代码改动。
   - 若是新业务，由相应负责人按照已定版的规范实施。
   - 若是历史遗留代码触发，优先级评估后纳入后续治理周期。

---

## 9. 历史版本与变更日志

| 版本 | 日期 | 治理范围 | 标志 |
| --- | --- | --- | --- |
| v1 | 2026-08-10 | 初版：问题盘点 | 架构规范定版 |
| v2 | 2026-08-15 | 中期阶段 0-5 验收 | 组合根、Module SPI、Plugin 拆解、Plugin ABI、DB 边界 |
| v3 | 2026-08-18 | 阶段 6-7 收口 | Application 端口、Runtime/Adapter 依赖清零 |

---

## 10. 验证脚本速查

以下脚本是验证各阶段完成的命令集：

```text
./.venv/bin/python -m pytest tests/test_architecture_dependencies.py -q
28 passed

python scripts/architecture/baseline.py --check --plugin-repo ../MoviePilot-Plugins
✓ Passed

./.venv/bin/python tests/run.py
4,914 passed、2 failed（Agent 图片能力测试，不相关）

../MoviePilot-Plugins/.venv/bin/python -m pytest tests/ci/test_v3_contract.py tests/ci/test_plugin_release_gate.py -q
16 passed
```

---

## 11. 附录：SDK / Compat / 运行时快照

### 11.1 SDK 导出清单

`app/sdk/` 及其 `__init__.py` 提供的公开导出列表（由 `scripts/sdk/exports.py --write` 维护）：

```python
# app/sdk/__init__.py 导出白名单示例
__all__ = [
    'request', 'get', 'post', ...  # app.sdk.network
    'cache', 'cache_async', ...  # app.sdk.cache
    'logger', ...  # app.sdk.logging
    'browser', 'render', ...  # app.sdk.browser
    # ... etc
]
```

完整清单见 `scripts/sdk/exports.py` 的 `ALLOW_LIST` 常数。

### 11.2 Compat Manifest 映射表

`app/runtime/compat/manifest.py` 定义的旧导入映射（按分类列举部分）：

```python
# app/runtime/compat/manifest.py 映射表示例
MANIFEST = {
    # PluginManager 及兼容导入
    'app.core.plugin.PluginManager': 'app.runtime.extensions.plugin_manager.PluginManager',
    'app.core.plugin.Plugin': 'app.runtime.extensions.registry.plugin.PluginRegistry.Plugin',
    ...
    # ModuleManager 及兼容导入
    'app.core.module.ModuleManager': 'app.runtime.extensions.module_manager.ModuleManager',
    ...
    # Oper 兼容导入
    'app.core.oper.SubscribeOper': 'app.db.oper.subscribe.SubscribeOper',
    ...
}
```

新增映射需遵守"宿主导出冻结"原则：canonical 模块中不复制旧名称，只在 manifest 中精确映射一次。

### 11.3 宿主交互协议（PluginBase）

内置 PluginBase（在 app/plugins 中可访问 `app.sdk.*`、`app.core.*`、event 等）：

```python
class PluginBase:
    """内置插件基类（提供给 app/plugins 中的插件）"""

    def get_name(self) -> str:
        """插件名称"""
        pass

    def get_version(self) -> str:
        """插件版本（如 "1.0.0"）"""
        pass

    def init_plugin(self):
        """插件初始化钩子（可选）"""
        pass

    def stop_plugin(self):
        """插件停止钩子（可选）"""
        pass

    @eventmanager.register(EventType.ConfigUpdated)
    async def on_config_update(self, event: Event):
        """配置变更钩子（可选）；处理器异常时不传播"""
        pass

    def get_api(self) -> APIRouter:
        """返回 FastAPI APIRouter；宿主会自动前置响应信封处理"""
        pass

    # Module 方法（若插件实现，需符合对应 Module 族的 Protocol）
    async def search_torrents(self, title: str, **kwargs) -> List[TorrentInfo]:
        """插件若实现，必须遵守 IndexerModule.search_torrents 签名"""
        pass

### 11.4 测试执行与边界覆盖

先运行本批次聚焦测试。涉及发布级公共行为时，使用仓库完整门禁：

```bash
python tests/run.py
```

本地若遇到已知二进制 `sites` 扩展导致的 `137/SIGKILL`，应按仓库既有测试 Stub 方案隔离；不能把进程被杀误报为断言失败，也不能因此跳过所有验证。

按变更边界追加测试：

| 变更边界 | 必测内容 |
| --- | --- |
| Event | 顺序、优先级、并发、handler 快照、目标插件、异常、热卸载 |
| Module | 插件优先、短路、列表合并、签名接力、sync/async、限流 |
| Plugin | hook 空值/异常、状态、配置、服务、API、页面、更新、热重载 |
| API | 路径、鉴权、响应信封/raw、状态码、OpenAPI、stream disconnect |
| DB | commit/rollback、并发、权限过滤、提交后副作用、同步/异步 |
| Startup | 主入口、`app.factory:app`、安全模式、部分失败、逆序关闭 |
| SDK/Compat | 旧路径、新路径、符号集合、对象身份、pickle/反射（如适用） |
| Agent | Provider 配置、工具 schema、流事件、取消、usage、插件工具 |

### 11.5 非功能回归

每个阶段至少记录，并将结果写入 `tests/fixtures/architecture/`：

- 冷导入 `app.factory` 耗时。
- 正常和安全模式生命周期耗时；当前基线使用 `scripts/startup/performance.py` 的 no-op 组件采样，明确不启动真实插件、网络或用户数据库。
- 隔离采样的线程数、后台任务数和数据库连接数范围；真实生产连接数由部署监控另行采集。
- 架构模块数、边数、自有 SCC、目标禁止边数量。
- 目标文件行数、方法最大行数、出度。

默认不要求每项立即变小，但不得无解释显著恶化。启动和请求关键路径超过 10% 的回归必须调查。

当前可复现命令：

```bash
python scripts/startup/performance.py --write --repeat 3
python scripts/architecture/baseline.py --check-host
python scripts/architecture/baseline.py \
  --check-plugins --plugin-repo ../MoviePilot-Plugins
```

### 11.6 2026-08-18 当前验证快照（收口批次）

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 后端完整门禁 | `./.venv/bin/python tests/run.py` | 4,914 passed、2 failed、3 skipped（2026-08-18）；失败为未修改的 Agent 图片能力测试，架构专项不受影响 |
| 架构与插件快照 | 分别运行 `--check-host` 与 `--check-plugins --plugin-repo ../MoviePilot-Plugins` | 已通过，基线已更新为 746 模块 / 6,024 边 |
| 前端联邦 API 客户端 | `yarn test:run src/api/__tests__/client.spec.ts src/api/__tests__/index.spec.ts` | 36 passed |
| 前端类型检查 | `yarn typecheck` | 通过 |
| V3 插件契约与版本门禁 | `../MoviePilot/.venv/bin/python -m pytest tests/ci/test_v3_contract.py tests/ci/test_plugin_release_gate.py -q` | 16 passed |
| 本次 IMDb/TVDB 插件适配 | `../MoviePilot/.venv/bin/python -m pytest tests/v3/imdbsource tests/v3/tvdbdiscover -q` | 14 passed |

架构专项复核：`tests/test_architecture_dependencies.py`、`tests/test_architecture_contract_baseline.py`、插件 API/注册/SDK 相关聚焦用例共 71 passed。全量门禁中的 2 个失败均来自未修改的 `tests/test_agent_image_capability.py`：其一依赖当前模型目录未提供的 MiniMax 图片能力元数据，其二直接调用消息链时未装配 Agent service；它们不是本批次的层间依赖或插件兼容回归。

独立插件仓 `tests/v3` 全量当前为 58 passed、13 failed（使用主仓 `.venv` 执行；插件仓自身 `.venv` 还缺少 `mutagen`，无法完成收集）。失败集中在本次未修改的 AnimeUpscale 版本断言、LibraryScraper 未知媒体源处理、历史身份迁移和媒体服务器身份测试；它们不经过本次 IMDb/TVDB 响应适配路径，但仍是插件仓自身需要单独清理的红色基线。不得把"本次适配专项通过"扩大表述为"插件仓全量通过"。

## 12. 量化治理目标

### 12.1 已达成的边界指标（阶段 0-2）

- 动态插件 API 返回契约明确并有真实请求测试。
- `run_module` 方法名和插件 hook 100% 进入契约快照。
- 自有 SCC 不增长，消除 `_music`/`subscribe`、schemas、DB 根回流等首批环。
