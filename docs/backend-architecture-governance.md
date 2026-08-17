# MoviePilot V3 后端架构提升与分阶段治理方案

> 文档性质：现状审计、目标约束、迁移路线和 AI 实施手册
> 适用仓库：`MoviePilot`，分支 `v3`
> 审计基线：2026-08-17 当前工作树
> 相关规范：`AGENTS.md`、`docs/rules/05-architecture.md`、`docs/architecture-overview.md`、`docs/backend-module-refactor-compatibility.md`

## 1. 文档目的

本文件不是另一份目录说明，也不是一次大规模重构设计稿。它解决四个更具体的问题：

1. 区分已经完成的物理目录迁移与仍未解决的职责、依赖和运行时契约问题。
2. 把问题定位到具体模块、类、方法和调用边界，给出可逐批落地的迁移方向。
3. 为其他 AI 提供可以直接执行的任务边界、兼容约束、验证命令和完成标准。
4. 在不破坏 V3 插件生态的前提下，逐步收敛宿主内部结构，而不是用一次性改名制造新的兼容层。

本文同时记录治理方案和当前工作树的实施状态。阶段 0 至阶段 5 已完成本轮中期验收所需的垂直切片；阶段 6 以后仍是后续路线。这里的“完成”只表示本轮验收边界已锁定，不表示所有 API、Chain、Agent 或兼容实现都已经长期收敛。每个阶段是否完成必须以本文件的机器基线、聚焦测试、插件兼容扫描和完整测试门禁为准，不能只凭目录已经创建判断。

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

当前的主要问题已不再是“文件放错目录”这么简单，而是以下八类结构性问题：

1. **规范比门禁严格。**现有测试能阻止核心实现层形成环，但允许 `chain`、`schemas`、`db`、Agent 子域和模块内部继续形成 SCC，也没有覆盖所有越层依赖。
2. **核心运行契约是字符串和约定。**`ChainBase.run_module()` 依赖方法名、签名探测、返回值形态和执行顺序；插件生命周期也依赖一组隐式 `get_*`/`init_*` 方法。它们是实际 ABI，却没有统一契约清单。
3. **编排类和端点承担过多职责。**订阅、搜索、整理、下载、Agent、插件管理、外部市场和服务端客户端均出现千行级文件、百行级方法和多种基础设施混合。
4. **数据库边界没有收口。**API、Chain、Scheduler、Application 直接依赖 ORM 模型或会话；模型本身又包含查询方法，和“统一经 Oper 访问”的目标不一致。
5. **组合根仍有泄漏。**全局单例、模块导入时创建 FastAPI app、事件解析器兜底实例化处理器、各 Chain 构造时自行抓取管理器，隐藏了依赖和所有权。
6. **Adapter、Application、Runtime 之间仍有反向依赖。**外部适配器直接读写 Oper，Runtime 插件管理器和服务注册直接读取系统配置，Application 消息能力直接引用 Agent 实现。
7. **插件兼容面大且缺少版本化。**旧导入、SDK、管理器具体类型、动态 API、事件装饰器、模块方法和热重载行为共同构成 ABI；目前主要靠兼容清单和测试样例保护。
8. **治理缺少可量化收敛目标。**测试绿只能说明已有规则没有被违反，不能说明巨型模块、隐式协议、直接数据库访问和内部环已经减少。

治理顺序必须是：**先冻结行为契约和补门禁，再拆环和依赖，再拆职责，最后才讨论缩减兼容面。**

## 4. 审计方法与当前基线

### 4.1 方法

本次基线使用以下方式获得：

- 读取仓库与后端架构规则。
- 运行 `tests/test_architecture_dependencies.py`。
- 复用该测试的 AST 模块解析逻辑，统计 `app/` 内部依赖；排除 `app/plugins/`。
- 统计文件规模、类和方法规模、入度、出度、SCC。
- 沿启动、事件、模块、插件、Chain、API、Oper、Agent 的真实调用路径阅读。
- 扫描独立插件仓的导入路径和插件钩子定义；不读取 `app/plugins/` 副本作为设计依据。

### 4.2 已验证结果

```text
./.venv/bin/python -m pytest tests/test_architecture_dependencies.py -q
26 passed
```

这只能证明当前代码符合现有门禁，不能证明符合本文件提出的更完整目标。

### 4.3 模块规模

排除 `app/plugins/` 后，当前静态扫描得到 707 个 Python 模块、6,096 条内部导入边。主要一级目录规模如下（代码行数包含注释和空行，用于趋势比较而非质量评分）：

| 一级目录 | 约代码行数 | Python 文件数 | 判断 |
| --- | ---: | ---: | --- |
| `app/modules` | 67,396 | 147 | 体量最大，包含大量具体平台模块和移植代码，需按模块族治理 |
| `app/agent` | 40,494 | 140 | Provider、工具、编排、策略均较重，应按子域治理 |
| `app/chain` | 29,663 | 36 | 文件不多但平均体量大，是优先拆分对象 |
| `app/api` | 16,745 | 42 | 多个端点含用例、持久化和流式协议实现 |
| `app/application` | 17,117 | 65 | 已承接多项用例，但部分仍是兼容 Facade 或反向依赖具体实现 |
| `app/runtime` | 13,976 | 48 | 插件注册/投影和事件运行时已拆出，宿主生命周期仍集中 |
| `app/adapters` | 12,721 | 35 | 插件市场、包、依赖和服务端入口已分出，旧 ABI 实现仍保留 |
| `app/db` | 8,181 | 49 | 根入口和模型兼容层已收敛，剩余局部环需后续治理 |
| `app/domain` | 7,654 | 21 | 相对可控，后续应继续保持纯语义 |
| `app/schemas` | 7,698 | 39 | 根入口已改为生成清单和惰性兼容导出 |

### 4.4 高出度模块

| 模块 | 静态出度 | 主要原因 |
| --- | ---: | --- |
| `app.agent.tools.factory` | 99 | 一次性导入全部内置工具并维护集中注册表 |
| `app.startup.modules_initializer` | 55 | 组合根职责，这是合理高出度，但仍需声明式管理 |
| `app.api.endpoints.system` | 54 | 系统设置、规则测试、日志、网络测试、运行控制混合 |
| `app.api.deps` | 49 | 认证、插件配置和跨端点依赖装配集中 |
| `app.agent.orchestrator` | 48 | Agent 构建、执行、工具、记忆、审计、用量混合 |
| `app.chain.message` | 48 | 消息路由和多个业务域耦合 |
| `app.chain.subscribe` | 48 | 写入、识别、搜索、匹配、完成、分享混合 |
| `app.chain.download` | 45 | 下载选择、客户端调用、字幕和历史混合 |
| `app.scheduler` | 42 | 调度定义、业务调用和运行控制仍混合，清理已迁出 |
| `app.chain.transfer` | 41 | 计划、执行、刮削、通知、回调、清理混合 |

`app.runtime`、`app.schemas`、`app.db` 等包入口具有很高入度。高入度本身不等于错误，但意味着它们是兼容和回归风险集中的枢纽，不能随意改变导出行为。

### 4.5 当前循环依赖

静态扫描共发现 9 个 SCC。首批 `schemas`、`db`、订阅音乐和 filemanager 目标环已消除，当前剩余环如下：

| SCC | 类型 | 优先级 | 处理原则 |
| --- | --- | --- | --- |
| `app.agent.llm`、`provider`、`helper`、`capability` | Agent 子域环 | P1 | 拆 Provider 元数据、协议适配、运行时与授权 |
| `app.agent.policy` 子模块环 | Agent 子域环 | P1 | 把 policy 数据、registry、sanitizer 依赖方向固定 |
| `app.doctor`、`app.monitor` 局部环 | 自有运行能力环 | P2 | 结合生命周期治理拆分 |
| `app.modules.qqbot` 局部环 | 平台模块局部环 | P2 | 模块内部单独处理 |
| `app.modules.telegram` 局部环 | 平台模块局部环 | P2 | 模块内部单独处理 |
| `app.modules.trimemedia` 局部环 | 平台模块局部环 | P2 | 模块内部单独处理 |
| `app.modules.ugreen` 局部环 | 平台模块局部环 | P2 | 模块内部单独处理 |
| `app.modules.themoviedb` 及其对象模型环 | 移植/第三方局部环 | 隔离 | 保持包内封闭，不让环越出模块边界，不优先重写 |

现有架构测试重点限制 `foundation/domain/runtime/adapters/application` 实现根和进程级跨包环，因此包内部的 `chain`、`schemas`、`db` 环仍能通过。后续门禁必须覆盖“自有代码 SCC 不增长”和“目标 SCC 逐项归零”。

### 4.6 阶段 0-5 实施后的机器基线

当前工作树重新生成 `tests/fixtures/architecture/dependency-baseline.json` 后得到：

| 指标 | 初始审计 | 当前基线 | 说明 |
| --- | ---: | ---: | --- |
| Python 模块数 | 约 654 | 707 | 增量主要来自单一职责的 Application、Runtime、Adapter 和维护用例模块 |
| 内部导入边 | 约 5,623 | 6,096 | 新增显式端口和组合连接后边数增加，不能单独把边数下降当目标 |
| SCC 数 | 14 | 9 | Schema、DB、订阅音乐、filemanager 等本轮目标环已消除 |
| `adapters -> db` | 存在 | 0 | `PluginHelper`、`MoviePilotServerHelper` 的本地数据读取已移到组合根/Application |
| `runtime -> db` | 存在 | 0 | 插件存储、服务配置均改为启动注入 |

剩余 9 个 SCC 位于 Agent LLM、Agent policy、Doctor/Monitor、TMDB 移植包及 QQBot、Telegram、TriMedia、UGreen 等模块内部，属于阶段 6 或隔离治理范围，不应为了宣布阶段 0-5 完成而仓促改写。

机器基线来源：

- `tests/fixtures/architecture/dependency-baseline.json`：模块、边、SCC 和目标边。
- `tests/fixtures/architecture/runtime-contract-baseline.json`：SDK、兼容清单、事件和 `run_module` 合同。
- `tests/fixtures/architecture/official-plugin-baseline.json`：独立官方插件仓 V2/V3 导入及钩子快照。
- `app/schemas/exports.py`：Schema 根入口的生成式兼容导出清单。

## 5. 目标架构与依赖方向

既有架构规则继续是规范来源。本文件补充的是可执行边界。

### 5.1 目标调用路径

```text
HTTP / CLI / Event / Scheduler / Plugin Hook
                |
                v
      Transport / Runtime Adapter
                |
                v
      Application Use Case / Chain Facade
                |
        +-------+--------+
        |                |
        v                v
   Domain Policy     Application Port
                         |
                         v
              Adapter / Oper / External Client
```

### 5.2 各层应承担的职责

| 层 | 应承担 | 不应承担 |
| --- | --- | --- |
| `foundation` | 无状态通用算法、值归一化、基础类型工具 | 配置、日志、数据库、HTTP、单例、业务流程 |
| `domain` | 媒体身份、规则、匹配、领域值和纯策略 | FastAPI、SQLAlchemy 会话、网络、调度器、插件管理器 |
| `runtime` | 事件循环、进程资源、扩展生命周期、执行上下文 | 具体业务查询、外部市场业务、页面 DTO 拼装 |
| `adapters` | HTTP、浏览器、文件系统、系统、命名外部服务的具体 I/O | 直接决定用例、直接持久化业务状态 |
| `application` | 有状态单能力、用例、端口协议、跨 adapter 的短流程 | 动态抓取全局管理器、长期进程生命周期、巨型多域编排 |
| `chain` | 面向用户目标的多域编排和向后兼容门面 | 直接写 SQL、实现底层协议、复制纯领域算法 |
| `modules` | 可替换宿主能力 Provider，实现模块 SPI | 反向控制 Chain、直接掌管宿主生命周期 |
| `api` | 参数解析、鉴权、传输 DTO、状态码、流协议 | 直接会话事务、调度细节、业务分支和外部上报 |
| `startup` | 唯一组合根、创建并连接实例、决定启停顺序 | 业务规则和常态请求处理 |
| `sdk` | 稳定、文档化、受测试保护的插件公开门面 | 随意导出内部单例和具体实现的新符号 |
| `runtime/compat` | 精确恢复旧路径和旧符号 | 承载新业务逻辑或模糊吞掉所有导入错误 |

### 5.3 强制依赖规则

后续新增代码应满足：

1. `foundation` 不依赖其他 MoviePilot 层。
2. `domain` 只依赖 `foundation` 和纯类型；必要 DTO 应移动到领域或契约模块，而不是依赖运行时 schema 聚合入口。
3. `runtime` 不直接依赖 `db.oper`、具体外部服务或 Chain。
4. `adapters` 不直接使用业务 Oper；外部结果通过返回值交给 Application 决定是否持久化。
5. `application` 不直接依赖 `api`、`startup`，不引用 Agent/Module 的具体类；通过 Protocol 或组合根注入。
6. `api` 不新增 `app.db.models`、`Session`、`AsyncSession`、`Scheduler`、`PluginManager` 的直接使用。
7. `chain` 不新增裸会话或直接 SQL，不新增通过延迟导入掩盖的环。
8. `modules` 可实现宿主 SPI，可调用稳定的 Application 能力，但不得让 Application 反向依赖具体模块类。
9. 只有 `startup` 和非常薄的兼容门面可以装配具体实现。
10. 兼容入口不反向成为宿主内部新代码的首选导入路径。

## 6. 详细问题与治理要求

### 6.1 架构规则与门禁存在空档

#### 现状证据

- `tests/test_architecture_dependencies.py` 已有 23 项测试，能保护虚拟兼容根、核心实现根和若干禁止边。
- 当前仍存在 `app.chain._music` ↔ `app.chain.subscribe`、`app.schemas`、`app.db` 等自有 SCC，说明门禁对包内部环有意留白。
- `app/application/messaging/skill.py:8` 直接导入 `app.agent.skills.registry`，说明“Application 不依赖具体 Agent 实现”的规则还没有全包覆盖。
- `app/adapters/external/market.py:33`、`app/adapters/external/server.py:12-14` 直接导入 Oper，说明 Adapter 禁止业务持久化的规则没有落到静态检查。
- 多个 API 端点直接导入 `Scheduler`、ORM 模型和数据库会话。

#### 风险

- 新改动只要没有触发已有少数模式，就可能继续扩大架构债务。
- “测试通过”容易被误读为“架构迁移完成”。
- 后续 AI 会复制当前调用方式，造成错误模式扩散。

#### 治理动作

1. 在现有测试中增加“趋势型门禁”，先用基线白名单锁住现状，再逐项减小白名单。
2. 对以下依赖设置零新增：
   - `app.adapters..* -> app.db..*`
   - `app.runtime..* -> app.db..*`
   - `app.api.endpoints..* -> sqlalchemy.orm.Session/sqlalchemy.ext.asyncio.AsyncSession`
   - `app.api.endpoints..* -> app.db.models..*`
   - `app.application..* -> app.agent..*`，唯一例外必须是明确稳定门面。
3. 增加自有 SCC 基线文件。白名单必须写明负责人、原因和目标阶段，不得只列模块名。
4. 对第三方/移植包使用路径级豁免，不使用整个 `app.modules` 豁免。
5. 每个架构批次输出变更前后：SCC、目标边数量、出度、受影响公开导入。

#### 完成标准

- 新代码不能增加上述禁止边。
- 每个阶段至少消除一个明确 SCC 或一类越层调用。
- 门禁失败信息打印“调用方、被调用方、允许的替代入口”。

### 6.2 `ChainBase` 是隐式服务定位器和字符串协议总线

#### 现状证据

- `app/chain/__init__.py:53-64` 中，每个 Chain 默认构造 `ModuleManager`、`EventManager`、`MessageOper`、`MessageHelper`、`MessageQueueManager`、`PluginManager` 和两种缓存。
- `run_module()` 位于 `app/chain/__init__.py:370-390`，先执行插件模块，再执行系统模块。
- 插件返回非空且不是列表时直接短路；列表结果继续合并。
- 系统模块按优先级执行；可能根据 `ObjectUtils.check_signature()` 把前一结果作为下一处理器唯一参数。
- AST 扫描发现约 211 个不同的字面量方法名、259 处调用。这已经是一套大型内部和插件协议，而不只是工具函数。

#### 不可破坏的行为

1. 插件模块先于系统模块执行。
2. 非空非列表结果短路。
3. 列表结果按现有规则合并。
4. 系统模块按 `get_priority()` 排序。
5. 同步方法在异步路径中进入线程池。
6. `raise_exception`、限流和系统错误通知语义保持。
7. 无参数 `Chain()` 构造仍可用，至少在 V3 兼容期内保持。

#### 目标设计

- 把调度算法提取为一个可单测的 `ModuleInvocationDispatcher`，只接收模块目录、插件模块目录、错误策略和执行器。
- 建立 `ModuleMethodContract` 清单，记录方法名、调用方式、参数模型、结果聚合策略、同步/异步能力、是否允许插件短路。
- `ChainBase` 保留兼容门面和公共辅助方法，但不再在每个实例构造时自行发现所有全局服务。
- 由 `startup` 创建 `ChainRuntimeContext`；无参构造从兼容 provider 取默认上下文，测试和新代码显式注入。
- 不把 211 个方法一次性改成枚举。先生成清单和测试，再按能力族引入 Typed Protocol。

#### 建议目标模块

```text
app/runtime/extensions/module/contracts.py
app/runtime/extensions/module/dispatcher.py
app/application/chain/context.py
app/chain/__init__.py                 # 保留 ChainBase 兼容门面
```

#### 完成标准

- 调度器可在不创建真实 PluginManager、ModuleManager、DB 和消息队列时独立测试。
- 现有 211 个方法名均被扫描清单覆盖，新增方法必须登记。
- 对插件优先、短路、列表聚合、签名接力、同步/异步、异常六类行为建立参数化契约测试。

### 6.3 巨型 Chain 混合了用例、策略、I/O 和展示副作用

#### 重点文件

| 文件 | 规模/热点 | 当前混合职责 | 首批拆分方向 |
| --- | --- | --- | --- |
| `app/chain/subscribe.py` | 约 3,794 行，70 个方法；`match()` 约 417 行 | 订阅写入、识别、搜索、匹配、缺失判断、完成、分享、历史、通知 | 命令、查询、匹配策略、完成策略、对外 Facade |
| `app/chain/search.py` | 约 2,901 行；结果解析约 195 行 | 搜索计划、站点并发、结果解析、规则过滤、流式回调 | 计划器、执行器、结果归一化、流式进度 |
| `app/chain/transfer.py` | 约 2,685 行；`do_transfer()` 约 885 行 | 计划、文件操作、刮削、历史、消息、媒体库刷新、回调 | 传输计划、执行、后处理、结果提交 |
| `app/chain/download.py` | 约 2,100 行；批量下载约 572 行 | 资源选择、客户端选择、提交、字幕、历史、通知 | 选择策略、提交服务、字幕流程、审计记录 |
| `app/chain/media.py` | 约 2,097 行 | 识别、缓存、身份转换、同步/异步重复 | 识别用例、身份解析、Provider 网关、缓存策略 |

#### 当前真实循环

`app/chain/_music.py:103-104`、`:134-135`、`:222-223` 通过延迟导入访问 `app.chain.subscribe` 的 `build_subscribe_meta`、`_subscribe_media_key`，而 `subscribe.py` 又导入 `MusicSubscribeMixin`。注释已经明确说明它是在回避模块级循环。

延迟导入只改变出错时机，不会恢复正确依赖方向。

#### 拆分原则

1. 保持 `app.chain.subscribe.SubscribeChain` 等公开路径和类名。
2. 优先提取纯函数和只依赖 DTO 的策略，再提取有状态用例。
3. 不在一次提交中同时改同步与异步全链路；先建立共享核心，再让两条入口委托。
4. 原 Facade 的参数默认值、返回类型、事件时机和消息副作用必须保持。
5. 不为了缩短文件把相互调用的方法机械分散到多个 `helper.py`。

#### 建议的订阅拆分

```text
app/domain/subscription/
  identity.py          # 订阅媒体键、稳定身份和纯比较
  matching.py          # 不访问 DB/网络的匹配规则
  completion.py        # 完整性与完成判定

app/application/subscription/
  commands.py          # 新增、修改、删除、完成
  queries.py           # 可见性和订阅读取
  recognition.py       # 通过端口恢复媒体信息
  search.py            # 搜索用例协调
  ports.py             # Repository、Search、Recognition、Event 等协议

app/chain/subscribe.py # V3 Facade，继续暴露 SubscribeChain 与旧辅助符号
```

`app/application/subscribe.py` 已经承担订阅写入翻译，可先作为新目录的入口门面，或保留并转发到新服务。不能同时出现同名文件和包；若最终改为包，必须在一个原子批次中完成，并验证 `app.application.subscribe` 的所有导入。

#### 建议的整理拆分

```text
app/domain/transfer/
  plan.py
  naming.py
  result.py

app/application/transfer_pipeline/
  planner.py
  executor.py
  metadata.py
  commit.py
  ports.py

app/chain/transfer.py  # 保持 TransferChain 兼容门面
```

`do_transfer()` 应先被改造成显式阶段流水线，每个阶段接受不可变上下文并返回新结果。不能在第一步就重写文件移动算法。

#### 完成标准

- 消除 `_music` ↔ `subscribe` SCC，不再用新增延迟导入维持。
- 目标大方法拆为有名称、可独立验证的阶段；单个用例方法原则上不超过 150 行。
- Chain Facade 的外部路径、方法名、参数和关键副作用测试保持。
- 每次只迁移一个垂直用例，例如“删除订阅”或“传输后处理”，不得一次搬完整个 Chain。

### 6.4 数据访问边界与事务所有权不一致

#### 现状证据

- `app/api/endpoints/subscribe.py:5-6` 直接导入同步/异步 Session，`:16-20` 直接导入 DB 入口、模型和 Oper，`:923-927` 直接执行删除、提交和回滚。
- 多个 API 端点直接依赖 `app.db.models`，包括 site、history、workflow、subscribe 等。
- Chain、Scheduler、Application 也存在模型直接引用。
- `app/db/models/subscribe.py:121` 起在 ORM 模型上定义查询方法，并通过 `@db_query` 等装饰器执行数据库访问。
- `app/db/__init__.py` 虽然已改为转发入口并惰性创建 Engine，但模型仍从 `app.db` 根入口回流导入装饰器和 Base，参与 DB SCC。

#### 问题本质

当前同时存在三种数据访问风格：

1. `db/oper` 服务。
2. ORM 模型类方法。
3. API/业务代码直接持有 Session。

这会让事务边界、权限过滤、事件发送和外部上报的先后次序散落在不同层。出现失败时很难判断哪些副作用已提交。

#### 目标边界

- ORM 模型只描述表、关系、约束和极少量无 I/O 的实体辅助。
- `db/oper` 是当前 V3 的持久化实现边界，不在本轮强制引入完整 Repository 框架。
- Application 用例拥有事务语义；API 只调用用例。
- 复杂跨 Oper 事务可引入小型 `UnitOfWork` Protocol，但不要为单表查询套通用框架。
- Event、Scheduler、Server 上报只在提交成功后触发；必要时用显式 after-commit 动作清单。

#### 迁移顺序

1. 统计宿主内部所有 `app.db.models` 和 Session 直接调用，建立基线。
2. 先迁移写操作，因为事务和副作用风险最高；读操作可稍后处理。
3. 为每个端点提取 Application command，例如 `DeleteSubscriptionCommand`。
4. Command 调用 Oper，并返回待发送事件/待调度动作；提交成功后执行。
5. 宿主内部调用切到 Oper 后，模型旧类方法继续保留为兼容转发，不在 V3 直接删除。
6. 内部 DB 模块改为从 `app.db.base`、`decorators`、`session` 精确导入，不经 `app.db` 根入口。

#### 插件兼容约束

- 独立插件仓仍有 `app.db.*`、`app.db.site_oper` 等直接导入。
- 旧模型类方法、`DbOper`、事务装饰器和惰性 `Engine`/`AsyncEngine` 符号不能因宿主内部收口而删除。
- 兼容转发不得改变同步/异步类型、装饰器提交行为和返回对象类型。
- 新 SDK 应提供更窄的数据/配置服务，但不能强迫现有插件同步迁移。

#### 完成标准

- `app/api/endpoints` 不再新增裸 Session 和模型写入。
- 第一阶段写端点全部由 Application command 负责事务。
- Adapter、Runtime 对 `app.db` 的直接依赖归零。
- DB 自有 SCC 消除；兼容根入口的外部导入测试保持通过。

### 6.5 启动组合根已经形成，但全局构造与隐式取实例仍然存在

#### 已有进展

`app/startup/modules_initializer.py:211-245` 已经承担托管资源、壁纸 Provider、认证载荷、DoH、站点、事件错误通知、模块、Agent 和前端的组合工作。`app/startup/lifecycle.py` 也显式规定数据库预热、路由、模块、插件、调度器、监控器、命令和工作流的顺序。这是正确方向。

#### 剩余问题

- `app/factory.py:328-333` 在模块导入时创建全局 FastAPI app 并注册给动态插件路由服务。
- `app/main.py` 在模块级创建 Server。
- `ChainBase` 构造时自行获取多个管理器和资源。
- `app/runtime/events.py:655-691` 在没有注册 resolver 时，尝试 `get_existing_instance()`，再兜底调用 `owner_class()`。这可能在事件到达时临时构造未托管对象。
- `eventmanager = EventManager()`、settings、global_vars 和多个 Singleton 形成事实上的服务定位器。
- 安全模式与正常模式的装配差异主要写在过程代码里，缺少可检查的组件清单。

#### 目标设计

```text
ApplicationRuntime
  - event_bus
  - module_registry
  - plugin_registry
  - scheduler
  - command_runtime
  - workflow_runtime
  - message_gateway
  - cache_registry
  - db_runtime
  - agent_runtime
```

- `startup` 创建一个 `ApplicationRuntime` 或等价的显式组件注册表。
- 生命周期步骤声明名称、依赖、start、stop、safe-mode 策略、超时和失败策略。
- 老的单例入口继续返回该注册表中的实例，保持对象身份。
- 新代码显式接收所需最小依赖，不接收整个容器。
- Event handler 必须由模块/插件/服务 resolver 解析。未绑定类的自动构造先告警并记录命中，完成迁移后改为拒绝。

#### 迁移要求

1. 先增加生命周期快照测试，记录正常模式、安全模式、关闭顺序和失败继续策略。
2. 再把单个资源改为注册表所有；一次只迁移一个资源。
3. 保留 `EventManager()`、`PluginManager()` 等现有入口的同一实例语义。
4. 禁止在迁移批次顺带改变 uvicorn/gunicorn 入口和 Docker 启动方式。
5. 测试 `app.factory:app` 直接挂载路径，因为它与 `main.py` 路径不同。

#### 完成标准

- 启动时能打印或导出已启用组件及其依赖顺序。
- Event handler 无未登记的运行时构造。
- 正常、安全模式、启动中断和部分关闭失败均有测试。
- 导入模块不建立数据库连接、不启动线程、不启动调度器。

### 6.6 事件总线同时承担注册、解析、调度、隔离和错误再广播

#### 现状证据

- `app/runtime/events.py` 约 801 行，包含装饰器注册、订阅快照、实例解析、同步/异步/广播调度、插件目标过滤、限流和错误通知。
- 链式事件按优先级顺序执行；广播事件通过线程池或 `asyncio.run_coroutine_threadsafe()` 并发执行。
- 广播事件对 `event_data` 仅做顶层浅拷贝；嵌套可变对象仍共享。
- `MessageAction` 使用 `__mp_target_plugin_id` 作为内部定向字段。
- 错误处理在通知后再次发送 `SystemError` 事件，存在错误处理链再次出错的递归风险。
- 未被 resolver 管理的类处理器可被临时实例化。

#### 必须冻结的语义

1. `EventType` 与 `ChainEventType` 的区别。
2. 链式事件的优先级、顺序和返回行为。
3. 广播事件的并发模型和“订阅快照从下一次事件生效”。
4. 插件定向消息不能被其他插件观察。
5. 同步处理器在线程池执行的条件。
6. 插件热加载/卸载时 handler 的启用和移除时机。

#### 目标拆分

```text
app/runtime/events.py                # 兼容门面与 eventmanager
app/runtime/event/registry.py        # 注册、快照、启停
app/runtime/event/binding.py         # resolver 与实例绑定
app/runtime/event/dispatch.py        # chain/broadcast 调度算法
app/runtime/event/errors.py    # 限流、错误隔离、通知降级
app/domain/events/                   # 逐步增加 Typed payload，不承载总线实现
```

#### 实施顺序

1. 为所有现有事件枚举生成 producer/consumer 清单。
2. 对高风险事件增加 payload model，但入口继续接受 dict，并在边界校验/转换。
3. 提取纯调度器，不改变 `EventManager` 公共方法和全局实例。
4. 为 resolver 未命中增加 DEBUG 诊断和测试；清零后移除自动构造兜底。
5. `SystemError` 增加递归保护和不可再次广播的降级日志路径。
6. 对需要深隔离的事件定义不可变 payload，不全局使用 `deepcopy`。

#### 完成标准

- 事件注册、实例绑定、调度和错误策略可分别测试。
- 高风险事件 producer/consumer 的 payload 契约一致。
- 热加载、定向插件、广播并发、错误递归保护均有回归测试。
- `app.core.event`、`app.sdk.events` 的对象身份和装饰器用法不变。

### 6.7 API 层包含用例、事务、调度和长流协议

#### 重点文件

| 文件 | 典型问题 |
| --- | --- |
| `app/api/endpoints/agent.py` | 约 2,315 行，`web_agent_stream()` 约 400 行，上传、队列、Agent 执行、SSE 映射和清理混合 |
| `app/api/endpoints/system.py` | 约 1,493 行，网络测试、规则测试、日志、配置、运行控制混合 |
| `app/api/endpoints/plugin.py` | 市场、安装、状态、详情、动态 API 注册和文件操作耦合 |
| `app/api/endpoints/subscribe.py` | 鉴权、查询、事务、事件、调度、共享上报混合 |
| `app/api/endpoints/site.py` | 站点 CRUD、认证、统计、图标和资源更新混合 |
| `app/api/endpoints/transfer.py` | `manual_transfer()` 约 293 行，解析、计划、执行和响应混合 |
| `app/api/endpoints/openai.py` | OpenAI 兼容协议、流式适配、业务执行混合 |

#### 目标设计

- endpoint 只负责传输参数、认证依赖、调用用例、映射响应。
- 业务权限检查进入 Application policy/use case；FastAPI 的 token 解码仍留在 API/security adapter。
- 后台任务不直接抓取 Scheduler 单例；调用 Application command 返回一个可提交的任务请求。
- SSE/OpenAI 流协议由独立 transport adapter 映射领域/Agent 事件。
- API 路径、HTTP method、状态码、响应模型和流事件格式保持。

#### 动态插件 API 的 P0 兼容冲突

当前 `app/factory.py:298-299` 把主应用默认路由类设为 `ResponseAPIRoute`；`app/application/plugins.py:87-104` 将插件返回的路由字典直接传给 `app.add_api_route()`。因此，未显式声明 raw 的动态插件 JSON 接口会进入主 API 的 `{success, message, data}` 包装逻辑。`tests/test_api_response.py:742-755` 目前甚至把这种行为固化为测试。

宿主的兼容原则应明确：**动态插件 API 保持插件自由返回，不强制使用主 API 统一响应信封。**这与主 API 的统一响应目标是两个边界，不能混为一谈。

阶段 0 必须完成以下之一，并由产品契约确认：

1. 动态插件注册时默认注入 `openapi_extra[RAW_RESPONSE_OPENAPI_KEY] = True`；插件显式请求统一信封时再开启包装。
2. 为动态插件创建专用 `PluginAPIRoute`，默认 raw，保留原生 `Response`、StreamingResponse 和插件自己的 Pydantic model。

同时补充真实请求级测试，不能只断言 route class 或 response model。

#### 完成标准

- 主 API 继续统一信封。
- 动态插件 API 的 raw 返回、原生 Response、文件/流响应和自定义状态码保持。
- 每个重点端点文件逐批只保留 transport 逻辑。
- API 层不再直接提交数据库事务或调用具体外部上报 Helper。

### 6.8 `PluginManager` 同时承担宿主生命周期、契约聚合、UI 投影和市场安装

#### 现状证据

`app/runtime/extensions/plugin_manager.py` 当前约 1,809 行、83 个方法，仍包含：

- 插件扫描、选择性加载、实例化、`init_plugin`、停止和热重载。
- 文件监控和本地变化处理。
- 配置和数据访问。
- 命令、API、服务、模块、动作、Agent tools 聚合。
- 页面、表单、侧栏、仪表板、授权 Provider 等 UI/交互投影。
- 插件状态、更新入口和兼容 Facade；市场、包、依赖的宿主调用已经改为经注入系统服务。

这使得 PluginManager 既是运行时 registry，又是 market service 和 presentation assembler。

#### 目标拆分

```text
app/runtime/extensions/plugin_manager.py       # 保留公共 Facade 和实例身份
app/runtime/extensions/plugin/lifecycle.py     # 后续提取 load/start/stop/reload
app/runtime/extensions/plugin/registry.py      # 实例、状态、元数据
app/runtime/extensions/plugin/contracts.py     # hook 解析与校验
app/runtime/extensions/plugin/projection.py    # commands/apis/services/modules/actions 投影
app/runtime/extensions/plugin/storage.py       # 运行时持久化窄端口
app/application/plugin/catalog.py              # 市场目录查询、代际合并和来源去重
app/application/plugin/install.py              # 安装用例与阶段结果
app/application/plugin/routes.py               # 动态 API 注册端口
```

#### 插件钩子契约

独立插件仓当前高频钩子包括：

| 钩子 | 扫描到的插件文件数（约） |
| --- | ---: |
| `init_plugin`、`stop_service`、`get_state`、`get_form`、`get_page`、`get_api` | 81-82 |
| `get_command` | 79 |
| `get_service` | 47 |
| `get_render_mode` | 11 |
| `get_dashboard` | 10 |
| `get_module` | 5 |
| `get_agent_tools` | 3 |

这些方法的存在性、参数、返回形态和异常隔离方式都是 ABI。目标 `plugin/contracts.py` 应定义 Protocol 和运行时 validator，但不能要求旧插件显式继承新 Protocol。

#### 实施顺序

1. 建立 hook contract snapshot，覆盖空值、错误值和异常。
2. 提取只读 registry，不改变加载流程。
3. 提取 projection，不改变前端 DTO。
4. 把市场和安装委托给 Application；PluginManager Facade 保留旧方法。
5. 最后才拆生命周期和文件 watcher，因为热重载风险最高。

#### 完成标准

- `PluginManager()` 仍返回同一实例，`app.sdk.plugins.PluginManager` 身份测试保持。
- 启停、更新、热重载、配置更新、动态路由刷新顺序不变。
- PluginManager 本身不再直接导入 DB、市场 client、pip、压缩包和备份实现；具体安装阶段由 Application command 和注入的包/依赖端口完成。
- 所有旧公共方法在 V3 保留，内部只做委托。

### 6.9 外部 Adapter 直接持久化并承载业务用例

#### `PluginHelper`

`app/adapters/external/market.py` 当前约 3,066 行、112 个方法，仍保留以下正式 V3 ABI 实现：

- 市场索引和发布信息请求。
- 插件包下载、解压、校验、备份和恢复。
- requirements 解析、冲突判断、pip 安装与降级策略。
- 同步/异步重复实现。
- 市场缓存、旧同步/异步安装入口和旧私有方法兼容。

它当前不再导入 `SystemConfigOper`；已拆出的 canonical 入口由 `PluginMarketClient`、`PluginPackageManager`、`PluginDependencyInstaller`、`PluginCatalogService` 和 `PluginInstallCommand` 承担。为了不破坏旧插件对 `PluginHelper` 的类名、静态方法和私有兼容调用，本轮没有把 3,066 行旧实现机械搬走，也没有在新模块中复制一套同名旧导出。后续阶段可继续把旧实现的具体算法逐步内移到这些组件。

建议拆为：

```text
app/adapters/external/plugin/client.py
app/adapters/system/plugin/package.py
app/adapters/system/plugin/dependency.py
app/application/plugin/catalog.py
app/application/plugin/install.py
app/adapters/external/market.py                 # PluginHelper 正式 ABI 与过渡实现
```

外部 client 只返回结构化结果；Application 决定版本选择、安装事务、备份和重载。

#### `MoviePilotServerHelper`

`app/adapters/external/server.py` 当前约 1,836 行、137 个方法，同时承担：

- 通用请求签名和 HTTP 调用。
- 使用统计和插件统计。
- 订阅、工作流、识别共享。
- 本地 Oper 查询和 payload 拼装。
- 多类响应解析与缓存。

当前文件不再直接导入 `SubscribeOper`、`SystemConfigOper` 或 `WorkflowOper`；本地数据读取和 payload 组装已由启动层注入的 `report.py`、`share.py` 用例提供。

建议拆为：

```text
app/adapters/external/server.py                 # HTTP transport 与旧 Helper Facade
app/application/server/report.py               # 插件/订阅统计和首次上报
app/application/server/share.py                # 订阅/工作流等分享用例
```

`server.py` 暂时同时保留底层 transport 和旧公开 Facade，但不再读取 Oper；启动组合根把数据读取 Provider、Application 用例和 transport 回调连接起来。后续如果 transport 继续增长，再建立 `app/adapters/external/server/` 主题目录并使用 `client.py`、`contracts.py` 等单词文件名，不能新增 `moviepilot_server.py` 一类多词实现模块。

#### 完成标准

- `app/adapters` 对 `app.db` 的静态导入为零。
- 外部 client 可用 fake transport 测试，不需要真实 DB。
- 业务用例可用 fake client 测试，不需要网络。
- 旧 Helper 路径和方法在 V3 内继续工作。

### 6.10 Schema 聚合入口和本地化产生运行时耦合

#### 现状证据

- `app/schemas/__init__.py` 已改为由 `app/schemas/exports.py` 驱动的惰性兼容入口；任意 `from app import schemas` 不再主动加载全部 schema 子模块。
- 仍需注意 `from app.schemas import X` 首次访问会加载该符号的所有者模块，不能把惰性入口误解为 schema 本身已经完全解耦。
- `app/schemas/dashboard.py:5`、`app/schemas/response.py:5` 直接依赖 `app.runtime.localization.LocaleHelper`。
- `Response.message` 的 Pydantic validator 在构造模型时读取当前请求 locale，序列化模型不再是纯数据操作。

#### 风险

- 小范围 schema 导入会触发大量模块加载，放大循环和启动时间。
- 同一个 Response 在不同上下文构造可能得到不同文本，后台任务和测试受 ContextVar/全局上下文影响。
- Domain/Application 依赖 schema 聚合入口时，被动依赖展示层和本地化运行时。

#### 目标设计

1. 宿主内部改用精确子模块导入。
2. `app.schemas` 根入口保留兼容，但用显式导出表和惰性 `__getattr__`，不再全量星号加载。
3. 建立导出符号冲突检查，避免不同 schema 同名时依赖导入顺序。
4. 本地化发生在 API/消息 presentation mapper，不发生在通用 DTO 构造阶段。
5. V3 内保持最终 API `message` 字段和语言行为，迁移时用请求级快照测试锁定。

#### 完成标准

- `app.schemas` 自有 SCC 消除。
- 内部新增代码不得 `from app.schemas import *`。
- 根入口公开符号集合有快照测试。
- schema 子模块不再依赖 `runtime.localization`；最终返回文本仍符合现有 locale 行为。

### 6.11 Application 仍依赖具体实现，能力边界不稳定

#### 典型证据

- `app/application/messaging/skill.py:8` 直接导入 `app.agent.skills.registry.SkillHelper`。
- `app/application/plugins.py` 直接持有 FastAPI app 并操作 `app.routes`、`openapi_schema` 和 `setup()`。
- 多个 `modules` 直接导入 `app.application.messaging.agent`、`mediaserver`、`storage` 等；其中一部分是合理 SPI 消费，一部分表明应用能力接口和具体实现未区分。
- `SystemConfigOper()` 在大量文件中被直接构造，形成持久化配置服务定位器。

#### 目标边界

- Application 能依赖自己定义的端口，不依赖 Agent registry、FastAPI app、PluginManager 具体类。
- 端口定义靠近消费者，例如 `SkillCatalog` 定义在 messaging use case 一侧，由 Agent adapter 实现。
- 动态路由操作应定义 `DynamicRouteRegistry` Protocol，FastAPI 实现在 API adapter，插件应用服务只提交路由描述。
- `SystemConfigReader/Writer` 作为窄协议注入用例，默认实现可继续包装 `SystemConfigOper`。
- Modules 只消费稳定 Application facade；需要长期保留的接口进入 SDK/Host SPI，而不是随意导入内部文件。

#### 完成标准

- `app.application` 不直接导入 `app.agent`、FastAPI 和具体 Module 类。
- Application 单测可通过 fake port 完成。
- Module 依赖的 Application 能力有 Protocol、生命周期和异常语义说明。

### 6.12 Agent 子系统存在集中注册、Provider 巨型对象和编排混合

#### 现状证据

- `app/agent/tools/factory.py` 静态出度约 99，一次性导入大量内置工具并维护集中列表。
- `app/agent/llm/provider.py` 约 3,527 行，内置 Provider 规格段约 700 行，并混合配置、授权、模型发现、协议兼容和运行实例创建。
- `app/agent/orchestrator.py` 约 3,116 行，混合 Agent 创建、执行、工具选择、用量记录、记忆和流式事件。
- `app/agent/llm/helper.py` 约 1,699 行，包含多种供应商兼容修补。
- Agent 已通过 `runtime_loader.py` 延迟物化，因此“让 Agent 延迟启动”不是下一阶段主要任务。

#### 目标拆分

```text
app/agent/llm/specs/              # Provider 静态规格，数据化并校验唯一 ID
app/agent/llm/auth/               # OAuth/设备码/会话状态
app/agent/llm/catalog.py          # 模型发现和缓存
app/agent/llm/protocols/          # OpenAI/Anthropic/Gemini 等适配
app/agent/llm/runtime.py          # 选定配置到运行客户端
app/agent/tools/manifests/        # 按能力域声明工具，不在工厂顶层全量导入
app/agent/execution/              # 执行、流事件、用量、恢复
```

#### 兼容要求

- Provider ID、配置 key、已保存授权状态、模型 ID 和默认选择不能变化。
- 工具名称、参数 schema、权限、用户确认语义不能变化。
- 插件 `get_agent_tools()` 和 `MoviePilotTool` 继承/注册机制保持。
- OpenAI 兼容 API 的事件顺序、finish reason、error 形态和 usage 保持。

#### 完成标准

- 工具工厂不再静态导入全部工具；按 manifest 或域 registry 延迟加载。
- `provider.py` 只保留兼容 Facade 和运行时入口。
- 每个 Provider 协议适配可单独做录制响应/fixture 测试。
- Agent 编排不直接处理 HTTP/SSE 格式。

### 6.13 `modules` 既是 Provider 集合，又出现模块内环和跨层扩散

#### 判断原则

`app/modules` 的高体量并不意味着应该整体改造成 Application。它是宿主可替换 Provider 的主要实现区，正确目标是：

- 每个模块实现明确 SPI。
- 模块自己的平台协议和对象留在模块内。
- 宿主只通过 ModuleManager/HostModuleAdapter 调用。
- 共享语义不藏在某个具体模块中。
- 模块不反向驱动 Chain 和宿主生命周期。

#### 当前重点

- `filemanager` 与 `transhandler` 形成双向依赖，应先提取传输 DTO、回调 Protocol 和文件操作结果。
- 消息平台模块重复依赖 `application.messaging.agent` 等能力，应固化消息网关 SPI，避免每个平台了解 Agent 细节。
- 媒体服务器模块直接消费 `application.mediaserver`，需要区分“宿主下发能力”与“模块反调宿主”的方向。
- TMDB 移植包内部大 SCC 应包内隔离，通过单一 Facade 对外，不开展无收益重写。

#### 完成标准

- 每个模块族有一份 SPI 清单和返回契约。
- 自有模块内部 SCC 逐项消除；第三方局部环不越过 Facade。
- ModuleManager 不再通过任意 `hasattr` 发现无限制能力；能力必须进入 method contract 清单。
- 插件 `get_module()` 仍可提供同名方法并参与现有聚合。

### 6.14 SDK 与兼容层是正式 ABI，但当前过宽

#### 当前事实

`app/runtime/compat/manifest.py` 当前约包含：

- 112 个模块别名。
- 1 个包别名。
- 8 个模块、约 41 个符号别名。
- 3 个虚拟包。

独立插件仓中仍高频使用：

| 导入面 | 使用文件数（约） |
| --- | ---: |
| `app.log` | 97 |
| `app.plugins` | 81 |
| `app.core.config` | 71 |
| `app.schemas.types` | 67 |
| `app.schemas` | 49 |
| `app.utils.http` / `app.utils.string` | 45 / 43 |
| `app.core.event` | 42 |
| `app.sdk.media` | 33 |
| `app.sdk.logging` | 24 |
| `app.sdk.config` / `app.sdk.network` | 20 / 18 |
| `app.core.context` | 17 |
| `app.helper.downloader` / `app.helper.sites` | 14 / 13 |
| `app.chain.download` / `subscribe` / `media` | 11 / 10 / 9 |
| `app.db.site_oper` | 10 |

现有 SDK 也直接导出 settings/global_vars、具体 PluginManager/ModuleManager、具体 EventManager 和多个跨层 Helper。它能维持兼容，但不是新插件应无限扩张依赖的依据。

#### 治理策略

1. 把 SDK 和兼容清单视为版本化公开产品，不是临时代码。
2. 建立 `sdk-public-api.json` 或等价测试清单，记录模块、符号、类型身份和行为测试。
3. 新增 SDK 能力优先导出 Protocol/Facade，不新增内部 manager 的可变状态。
4. 宿主内部不因兼容存在而继续使用旧 `app.core.*`、`app.helper.*`、`app.utils.*` 路径。
5. V3 默认只增不删。弃用必须包含：替代入口、诊断、至少一个完整发布周期、官方插件仓扫描、样例第三方插件验证。
6. 兼容模块必须精确路由，不能用宽泛 `__getattr__` 吞掉拼写错误。
7. 需要保持类/单例身份的符号必须测试 `is`，不能只测试能导入。

#### 完成标准

- SDK 公开面有机器可读清单和变更审查。
- 每次迁移明确列出旧路径、新路径、身份要求和保留期限。
- 独立插件仓 v2/v3 静态导入扫描通过。
- V3 治理批次不删除现有 112/41 兼容项。

### 6.15 配置、缓存和错误策略分散

#### 现状

- `settings` 在大量模块中直接读取，这是运行配置的事实 API。
- `SystemConfigOper()` 在几十个文件中直接构造，运行配置与持久化用户配置边界模糊。
- 缓存装饰器、文件缓存、Redis、内存状态由调用方自行选择，缺少能力级一致失效策略。
- 部分层把异常转成 `schemas.Response`，部分抛异常，部分发送 `SystemError`，部分只记录日志。

#### 目标

- `settings` 仅表示启动时环境配置；用例接收所需配置快照，而不是读取整个 settings。
- 持久化系统配置通过窄 `SystemConfigReader/Writer`。
- 每个能力明确缓存所有者、key、TTL、负缓存、失效事件和降级策略。
- Domain/Application 返回领域错误；API 映射 HTTP；Event/Background runtime 决定重试、通知和死信。
- 不在第一阶段引入统一“万能 Result”类型；先统一错误所有权。

## 7. 插件兼容治理专章

### 7.1 插件是外部消费者，不是内部实现目录

本次后端重构必须同时接受两个事实：

1. `app/plugins/` 是运行时副本，不能按其当前内容决定宿主架构。
2. 插件运行时仍依赖宿主提供的 `_PluginBase`、旧导入、SDK、事件、模块、API、调度和配置能力，这些必须作为黑盒 ABI 保护。

兼容审计至少包含：

- 独立官方插件仓 `plugins.v2/`、`plugins.v3/`。
- `runtime/compat/manifest.py`。
- `app/sdk/` 公开导出。
- PluginManager 实际消费的 hook。
- `tests/test_legacy_import_compat.py`、`tests/test_legacy_plugin_resource_imports.py`、`tests/test_plugin_sdk.py` 等。
- 一组最小第三方插件 fixture，覆盖旧导入、事件、动态 API、模块、服务和 Agent tool。

### 7.2 必须保持的兼容维度

| 维度 | 必须验证 |
| --- | --- |
| 导入 | 旧模块和旧符号可导入；包/模块形态与子模块导入不冲突 |
| 身份 | Singleton、Manager、EventManager、公开类在旧新路径下按要求保持 `is` |
| 构造 | 插件基类和 Chain 的无参构造仍工作 |
| Hook | 方法名、参数、同步/异步、None/空列表语义、异常隔离不变 |
| Module | 插件优先级、短路、列表合并、签名接力语义不变 |
| Event | 注册装饰器、目标插件过滤、链式顺序、热卸载清理不变 |
| API | 路径、鉴权默认值、raw 返回、原生 Response、流式返回不被主 API 信封改变 |
| Service | 定时任务描述、Cron、启动/停止和去重语义不变 |
| UI | form/page/dashboard/sidebar DTO 形态不变 |
| Data | 插件配置和 PluginData 的 key、序列化、隔离和迁移行为不变 |
| Reload | 本地开发 watcher、更新、备份、重新实例化和路由刷新顺序不变 |

### 7.3 兼容迁移模式

每个公开模块迁移采用以下模式：

```text
旧入口（永久或长期 Facade）
      |
      v
新 Application/Runtime/Adapter 实现
      ^
      |
startup 注入具体依赖
```

规则：

1. 先新增实现和契约测试。
2. 旧入口改为薄委托，但保留公开名称。
3. 宿主内部调用切换到新入口。
4. 官方插件无需修改即可通过。
5. 新 SDK 入口可逐步推广，但不以删除旧路径作为同一批次完成条件。
6. 若类的 `__module__`、pickle、反射或前端模块名会变化，必须显式增加兼容测试。
7. 已废弃的模块路径统一登记到 `app/runtime/compat/manifest.py`，不得在新实现模块内复制导出旧对象。
8. `PluginManager`、`PluginHelper`、`MoviePilotServerHelper` 等仍被插件直接依赖的正式公共路径必须保留原有公共合同和对象身份；其中已经完成职责拆分的入口可以委托新实现，但尚未迁出的算法仍可能留在原类中，不能把它们笼统描述成纯薄 Facade。
9. 新实现包默认不增加 `__all__`、惰性 `__getattr__` 或模块级旧类别名；确需公开时进入 `app/sdk` 导出清单和架构快照。

### 7.4 插件兼容禁止事项

- 不扫描 `app/plugins/` 后批量改写插件源码。
- 不把插件 API 自动包装成主 API 统一信封。
- 不改变 `get_module()` 返回字典的方法名或 `run_module()` 聚合顺序。
- 不因新 Protocol 存在就要求旧插件继承它。
- 不把热重载问题用“重启生效”替代。
- 不把 SDK 改成全新对象，导致旧路径和新路径的 Singleton 身份分裂。
- 不在 V3 普通架构 PR 中删除兼容 manifest 项。

## 8. 分阶段实施路线

每个阶段可以拆成多个小 PR/提交。阶段之间有依赖，阶段内部按风险从低到高推进。

### 阶段 0：冻结契约、纠正 P0 兼容边界

#### 目标

先知道什么不能变，并修复会阻碍后续治理的契约冲突。

#### 工作项

1. 生成并提交当前架构基线：模块、导入边、自有 SCC、禁止边白名单。
2. 生成 `run_module` 方法清单，覆盖约 211 个方法名及调用位置。
3. 生成插件 hook、SDK 导出、compat manifest 和官方插件导入快照。
4. 增加动态插件 API 真实请求测试，恢复/确认 raw free-return 边界。
5. 增加启动矩阵：`app.factory:app`、主入口、安全模式、正常模式、关闭失败。
6. 增加 Event/Module/PluginManager 对象身份测试。
7. 记录当前导入耗时和启动关键阶段耗时，作为后续非功能基线。

#### 不做

- 不拆巨型文件。
- 不移动公开类。
- 不删除兼容映射。
- 不改数据库结构。

#### 验收

- 行为契约成为测试或机器可读清单。
- 动态插件 API 的返回边界有明确、可执行测试。
- 架构基线可以在 CI 中稳定复现。

### 阶段 1：补架构门禁并消除低风险环

#### 目标

先让依赖图停止恶化，再处理不涉及业务算法的环。

#### 工作项

1. `app.schemas` 改为显式/惰性兼容导出；宿主内部使用精确子模块导入。
2. 移除重复 `system` 导出，增加公开符号快照和冲突检查。
3. DB 内部模块从具体 `app.db.base/decorators/session/engine` 导入，不经根入口回流。
4. 消除 `app.chain._music` ↔ `subscribe`：把订阅媒体 key、meta 构造移到 Domain/Application 的单向依赖模块。
5. 消除 `filemanager` ↔ `transhandler`：提取共享 DTO/Protocol。
6. 新门禁设为禁止新增 Adapter→DB、Runtime→DB、API→Session/Model、Application→Agent 具体实现。

#### 兼容方式

- `app.schemas.X` 和 `app.db` 旧导出继续工作。
- `app.chain.subscribe` 的旧辅助函数保留转发，直到插件扫描证明可移除；V3 默认不移除。
- 文件改包时保持完整导入路径和类名。

#### 验收

- 自有目标 SCC 至少减少 3 个。
- 新门禁无无期限宽泛豁免。
- 官方插件仓静态导入和宿主兼容测试通过。

### 阶段 2：显式运行时组合与事件/模块调度契约

#### 目标

把隐藏在 Singleton、装饰器和字符串里的宿主运行机制变成可组合、可测试的基础设施。

#### 工作项

1. 提取 `ModuleInvocationDispatcher`，由 `ChainBase` 委托。
2. 引入 method contract registry，先覆盖高频能力族。
3. 提取 Event registry、binding resolver、dispatcher、error policy。
4. 引入生命周期组件描述，逐个登记 start/stop/safe-mode/timeout。
5. Event resolver 未命中增加诊断；迁移宿主 handler 到显式 resolver。
6. 让 Chain 新代码可注入 `ChainRuntimeContext`，保留无参兼容 provider。

#### 风险控制

- 先复制现有算法到可测试组件，再让 Facade 委托，不能边提取边重写规则。
- 同步和异步聚合测试必须成对。
- 对广播事件使用可控 executor 和 loop fixture。
- PluginManager/EventManager/ModuleManager 身份保持。

#### 验收

- 调度算法不依赖真实插件、数据库和线程即可单测。
- Event handler 不再由总线隐式构造，或剩余命中有明确白名单和日志。
- 生命周期顺序由测试锁定。

### 阶段 3：数据访问与 API 用例收口

#### 目标

让事务、权限和提交后副作用拥有清晰所有者。

#### 工作项

1. 从订阅删除/修改、站点修改、工作流修改等写端点开始，建立 Application command。
2. 把 ORM 直接写入、commit/rollback、事件、调度、外部上报迁入用例。
3. 建立必要的 Oper/UnitOfWork 端口。
4. 模型类方法在宿主内部逐步停用，保留兼容转发。
5. 端点只做 FastAPI 参数和结果映射。
6. Scheduler 的数据库清理逻辑迁到 Application maintenance use case，Scheduler 只触发。

#### 推荐垂直切片顺序

1. 删除订阅。
2. 手工触发订阅搜索。
3. 站点启停/修改。
4. 工作流启停/删除。
5. 历史删除与清理。
6. 插件状态与配置更新。

每个切片单独验证，不等待所有端点一起完成。

#### 验收

- 已迁移端点不持有 Session、不直接调用 Model/Oper/Scheduler/ServerHelper。
- commit 失败时不发送成功事件、不上报、不调度后续任务。
- 同步/异步路径和权限结果不变。

### 阶段 4：按用例拆分巨型 Chain

#### 目标

在数据和运行时边界已经稳定后，拆解业务编排。

#### 工作项

1. Subscribe：身份、命令、识别、搜索、匹配、完成。
2. Search：计划、并发执行、归一化、过滤、流式进度。
3. Transfer：计划、执行、元数据、提交、后处理。
4. Download：候选选择、客户端提交、字幕、审计。
5. Media：身份解析、Provider 识别、缓存和同步/异步共核。
6. Message：通道解析、路由、交互状态和业务 handler。

#### 拆分方式

- 每次选一个公开方法作为纵向切片。
- 先做 characterization test。
- 新服务返回结构化结果，Facade 负责兼容旧返回。
- 事件、通知、历史和缓存失效点写入时序测试。
- 纯策略下沉 Domain；短用例进入 Application；多域串联保留 Chain。

#### 验收

- 目标 Chain 文件规模和出度持续下降。
- 不新增 `misc.py`、`common.py`、`helper.py` 式无边界收纳文件。
- Facade 兼容测试覆盖所有被迁移公开方法。

#### 阶段 0-4 当前落地索引

| 阶段 | 已落地入口 | 已锁定的关键语义 |
| --- | --- | --- |
| 0 | `scripts/architecture/baseline.py`、`scripts/schema/exports.py`、三份 architecture fixture | 模块/边/SCC、SDK/compat、事件、`run_module`、官方插件 V2/V3 导入和钩子快照 |
| 0 | `app/adapters/web/plugin/routes.py`、`app/application/plugin/routes.py` | 主程序继续统一 envelope；动态插件 API 默认 raw，自定义状态码、原生 Response、文件/流响应不被改写 |
| 0 | `MoviePilot-Frontend/src/api/client.ts` | 联邦插件公共客户端遇到非 `Response` payload 时原样返回；合法 envelope 仍保留统一错误反馈 |
| 1 | `app/schemas/exports.py`、`app/schemas/__init__.py` | Schema 根入口惰性兼容导出，宿主内部使用精确子模块，公开符号由生成清单锁定 |
| 1 | `app/application/subscription/contract.py`、`app/modules/filemanager/module.py` | 订阅身份/元数据和文件管理共享合同改为单向依赖，目标 SCC 不再靠延迟导入维持 |
| 1 | `tests/test_architecture_dependencies.py`、dependency baseline | Adapter/Runtime 到 DB 零新增，API/Session/Model 与 Application/Agent 采用趋势基线治理 |
| 2 | `app/runtime/extensions/module/contracts.py`、`dispatcher.py` | 插件优先、短路、列表合并、参数签名和同步/异步执行顺序保持 |
| 2 | `app/runtime/event/{registry,binding,dispatch,errors}.py` | 事件注册、实例解析、分发和错误降级拆开；总线不再隐式构造未绑定处理器 |
| 2 | `app/application/chain/context.py`、`app/startup/lifecycle/components.py`、`scripts/startup/performance.py` | Chain 依赖可注入；正常/安全模式启停顺序、超时、阶段耗时及隔离资源快照可导出测试 |
| 3 | `app/db/uow.py`、`app/application/subscription/{delete,identity}.py` | 订阅删除事务、权限、提交后事件/上报时序归 Application 所有；端点只做传输映射 |
| 3 | `app/application/subscription/query.py`、`app/application/maintenance.py`、`app/db/maintenance.py` | 订阅查询三条垂直切片和六张维护表的保留期/批次/失败汇总归 Application；Scheduler 只触发 |
| 4 | `app/application/search/state.py` | 搜索状态查询和控制从巨型 Chain 提取，保留原同步/异步状态语义 |
| 4 | `app/application/download/tasks.py` | 下载任务查询/控制形成窄用例，Chain 保留用户目标编排 Facade |
| 4 | `app/application/music/catalog.py` | 多来源音乐目录聚合形成可用 fake Provider 测试的应用服务，不改变原搜索命中/回退行为 |
| 4 | `app/application/transfer.py`、`app/application/messaging/session.py` | Transfer、Message 各三条以上状态/控制切片由窄服务承接，旧 Chain 方法保留兼容委托 |

这些切片只代表阶段 0-4 的低风险中期目标，不表示全部 API、Chain 和模型访问已经完成长期收口。当前基线仍有 42 条 API endpoint→Model、15 条 endpoint→Session、3 条 Application→Agent 具体实现边；它们是后续垂直切片的明确欠账，不能通过扩大白名单消除告警。

### 阶段 5：拆分插件宿主与外部服务适配

#### 目标

让 PluginManager 只管理扩展运行，让 Adapter 只做 I/O。

#### 工作项

1. Plugin hook contract/registry/projection 从 PluginManager 提取。
2. 插件市场查询和安装进入 Application 用例。
3. `PluginHelper` 拆 market client、包管理、依赖安装。
4. `MoviePilotServerHelper` 拆 transport client 与分享/统计用例。
5. 动态路由以 `DynamicRouteRegistry` 端口连接 FastAPI adapter。
6. Runtime 的系统配置访问改为启动注入的 reader。

#### 验收

- Runtime 和 Adapter 不再导入 DB Oper。
- PluginManager 仍保持完整 V3 公共方法和实例身份。
- 插件安装失败可以明确回滚文件、依赖、实例和路由中的哪些步骤。
- 热重载与在线更新测试覆盖。

#### 当前已落地切片

| 职责 | Canonical 实现 | 旧入口/兼容方式 |
| --- | --- | --- |
| 插件钩子契约 | `app/runtime/extensions/plugin/contracts.py` | 旧插件仍按鸭子类型实现，不要求继承 Protocol 或基类 |
| 插件类与运行实例注册 | `app/runtime/extensions/plugin/registry.py` | `PluginManager.plugins`、`running_plugins` 仍返回原有可变映射 |
| 命令/API/服务/模块/动作/联邦/认证/侧栏/仪表板元数据投影 | `app/runtime/extensions/plugin/projection.py` | `PluginManager.get_plugin_*()` 原方法委托，异常隔离和 DTO 不变 |
| 插件配置和数据持久化 | `app/runtime/extensions/plugin/storage.py` | 启动层用 `SystemConfigOper`、`PluginDataOper` 注入；Runtime 不导入 Oper |
| 市场目录和版本/来源合并 | `app/application/plugin/catalog.py` | `PluginManager.get_online_plugins()` 等公开方法经启动注入的目录工厂委托 |
| 插件安装阶段编排 | `app/application/plugin/install.py` | API 和 Agent 共用命令；旧管理器/Helper 安装入口保留 |
| 动态插件路由 | `app/application/plugin/routes.py` + `app/adapters/web/plugin/routes.py` | `app/application/plugins.py` 保留旧 Facade；插件响应默认 raw |
| 市场读取 | `app/adapters/external/plugin/client.py` | `app.adapters.external.market.PluginHelper` 保留正式公共实现路径 |
| 包与依赖安装 | `app/adapters/system/plugin/package.py`、`dependency.py` | PluginManager 原方法只做委托和日志/上报 |
| 中心服务统计/分享 | `app/application/server/report.py`、`share.py` | `MoviePilotServerHelper` 保留 transport 和公开静态/类方法，由启动层注入用例 |

阶段 5 的“拆分”是职责入口和组合依赖的拆分，不等于本轮把旧 `PluginHelper` 的全部 3,066 行算法复制到新文件。旧类仍是正式 V3 ABI，保留原类名、对象/静态方法和旧私有调用；新宿主路径使用上述 canonical client、package、dependency 和 Application command。后续如需继续内移算法，必须先增加旧私有调用命中统计和逐方法行为快照。

这里的“兼容”分为两类，后续 AI 不得混淆：

1. 已迁移、只需恢复旧模块路径的入口，统一登记到 `app/runtime/compat/manifest.py`，新实现模块不复制旧对象导出。
2. 插件直接依赖其对象身份或静态方法的正式 ABI，如 `PluginManager`、`PluginHelper`、`MoviePilotServerHelper`，继续留在原路径；已拆出的职责由 canonical 组件承接，未迁出的实现仍由原类承担。它们不是在新模块里额外定义一份别名，也不能为了“看起来统一”复制一套旧类。

`app.sdk.plugins` 只显式导出 `ModuleManager`、`PluginManager`。阶段 5 新实现包没有增加 `__all__`、惰性 `__getattr__`、旧 Manager/Helper/Oper 别名；任何新增插件公开能力必须先进入 SDK 清单和快照测试。

### 阶段 6：Agent 与模块族治理

#### 目标

处理高体量但相对独立的垂直子系统，避免阻塞前面主链路治理。

#### 工作项

1. Provider 规格数据化并与授权、模型目录、协议 client 分离。
2. Agent 执行事件与 HTTP/SSE 映射分离。
3. 工具注册按能力域延迟加载，降低工厂出度。
4. 消息模块、媒体服务器模块、下载器模块分别固化 SPI。
5. 消除自有模块内部 SCC；隔离第三方包局部环。

#### 验收

- Provider ID/配置和工具 schema 快照不变。
- 工具工厂出度显著下降，目标不高于 20。
- Agent 单元测试不需要启动完整 MoviePilot runtime。
- 模块族可以用 host contract fixture 独立验证。

### 阶段 7：SDK 收敛、兼容治理与长期预算

#### 目标

让兼容从“永久扩张”变成“有版本、有观测、有替代入口”的产品能力。

#### 工作项

1. 发布 SDK public manifest 和变更规则。
2. 为旧入口增加 DEBUG 级命中统计，不记录插件敏感数据。
3. 标记推荐的新 SDK Facade；文档和新官方插件优先使用。
4. 建立弃用决策模板，但 V3 普通版本不删除旧映射。
5. 将架构指标纳入 CI 报告：SCC、禁止边、目标直接 DB 调用、巨型文件、SDK 变化。

#### 验收

- 新插件可以只依赖 SDK/Host SPI 完成常见能力。
- 旧插件无需修改继续工作。
- 每个弃用项有真实命中数据和替代方案，不按时间自动删除。

## 9. 推荐的首批实施任务

以下任务粒度适合其他 AI 独立执行，并且互相依赖清晰。

### 任务 A：插件动态 API raw 契约

**范围**：`app/application/plugins.py`、`app/api/response.py`、`app/factory.py`、对应测试。
**目标**：主 API 统一信封，插件动态 API 默认自由返回。
**禁止**：修改插件副本、修改普通 API 响应格式、修改鉴权默认值。
**验证**：dict、Pydantic model、Response、StreamingResponse、204、自定义状态码、OpenAPI。

### 任务 B：`_music`/`subscribe` 环拆除

**范围**：`app/chain/_music.py`、`app/chain/subscribe.py`、订阅身份相关 Domain/Application 文件和测试。
**目标**：迁移 `build_subscribe_meta`、`_subscribe_media_key(s)` 的真正所有权，消除延迟导入。
**禁止**：改变音乐搜索、订阅完成判定、媒体身份字段、旧辅助函数路径。
**验证**：音乐单曲/专辑、缺少远端 ID、同步/异步识别、旧路径导入、SCC。

### 任务 C：Schema 根入口惰性兼容导出

**范围**：`app/schemas/__init__.py`、内部精确导入、导出清单和测试。
**目标**：消除全量星号导入和 schema SCC。
**禁止**：删除 `app.schemas.X`、改变 Pydantic schema 和 OpenAPI。
**验证**：公开符号快照、重复名、冷导入、全部 schema model rebuild、API OpenAPI。

### 任务 D：Chain 模块调度器提取

**范围**：`app/chain/__init__.py`、`app/runtime/extensions` 新调度组件、契约测试。
**目标**：原样提取插件/系统模块调度算法。
**禁止**：改变执行顺序、异常、限流、聚合、线程池策略。
**验证**：参数化契约矩阵及 PluginManager/ModuleManager fake。

### 任务 E：订阅删除垂直切片

**范围**：`app/api/endpoints/subscribe.py` 删除端点、Application command、Oper 和测试。
**目标**：API 不直接管理事务；提交成功后才发送事件和上报。
**禁止**：改变路由、权限、响应、媒体身份、事件 payload。
**验证**：存在/不存在、普通用户、管理员、commit 失败、事件失败、上报失败。

### 任务 F：外部服务 client 与分享用例分离

**范围**：`app/adapters/external/server.py` 选一个低风险能力，例如 workflow 分享。
**目标**：client 不导入 Oper，Application 负责数据读取和 DTO。
**禁止**：一次拆完整个 1,900 行文件。
**验证**：旧 Helper 方法、请求参数、缓存、错误降级和 fake transport。

## 10. AI 实施标准作业流程

其他 AI 接到本文件中的任务时，必须按以下顺序执行。

### 10.1 开始前

1. 读取根 `AGENTS.md`、仓库 `AGENTS.md` 和所涉及目录规则。
2. 检查分支、工作树、上游差异；不得覆盖用户或其他进程改动。
3. 阅读公开入口、所有调用方、相关测试和兼容 manifest。
4. 如果涉及插件契约，扫描 `../MoviePilot-Plugins/plugins.v2` 与 `plugins.v3`；不要把 `app/plugins` 当源码。
5. 记录迁移前静态依赖、公开符号和行为快照。

### 10.2 任务说明必须包含

```yaml
objective: 单一可验证目标
scope:
  allowed_files: []
  affected_modules: []
out_of_scope: []
current_evidence: []
public_contracts:
  imports: []
  methods: []
  events: []
  api: []
plugin_compatibility:
  old_paths: []
  identity_requirements: []
  runtime_behaviors: []
migration_steps: []
tests:
  focused: []
  architecture: []
  compatibility: []
rollback: 如何恢复委托而不丢数据
done_when: []
```

### 10.3 编码规则

1. 一个批次只修一个依赖方向或一个垂直用例。
2. 先建新实现，再让旧入口委托；不能先删除旧入口。
3. 新增类和方法按仓库规则写类级、方法级中文注释，说明原因和关键约束。
4. 注释不能只复述代码；兼容转发必须注明保留原因和不可改变的语义。
5. 不用延迟导入作为最终环修复；它只能作为短期过渡且必须有清理任务。
6. 不引入 `Manager2`、`HelperNew` 等无所有权名称。
7. 不创建通用 `common.py`/`misc.py` 收纳不相关逻辑。
8. 同步和异步逻辑优先共享纯核心，不用复制粘贴维持两套算法。
9. 不把异常全部捕获后返回 False；错误类型和降级责任由边界决定。
10. 不顺带格式化或重排无关大文件。
11. 新增生产 Python 模块的文件名只使用一个小写单词；同一主题需要多个模块时，建立主题子目录，并在其中使用单词文件名。
12. 已存在的多词公开导入路径只有在插件或兼容扫描证明不能迁移时才保留为薄门面，不得继续作为新模块命名模板。
13. 测试文件继续使用 pytest 的描述性 `test_<behavior>.py` 命名，不受生产模块单词命名约束。

### 10.4 每次迁移的七步闭环

1. **刻画**：补现有行为测试。
2. **建契约**：定义 Protocol、DTO 或机器可读清单。
3. **提取**：不改行为地移动单一职责。
4. **委托**：旧入口调用新实现。
5. **切换**：宿主内部新代码改用 canonical 入口。
6. **兼容**：运行旧导入、对象身份和插件 fixture。
7. **度量**：报告环、禁止边、出度、文件规模的变化。

任何一步没有验证，任务都不能标记为完成。

## 11. 验证矩阵

### 11.1 每个架构批次的最低门禁

```bash
./.venv/bin/python -m pytest tests/test_architecture_dependencies.py -q
./.venv/bin/python -m pytest tests/test_legacy_import_compat.py -q
./.venv/bin/python -m pytest tests/test_legacy_plugin_resource_imports.py -q
./.venv/bin/python -m pytest tests/test_plugin_sdk.py -q
```

再运行本批次聚焦测试。涉及发布级公共行为时，使用仓库完整门禁：

```bash
./.venv/bin/python tests/run.py
```

本地若遇到已知二进制 `sites` 扩展导致的 `137/SIGKILL`，应按仓库既有测试 Stub 方案隔离；不能把进程被杀误报为断言失败，也不能因此跳过所有验证。

### 11.2 按边界追加的测试

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

### 11.3 非功能回归

每个阶段至少记录，并将结果写入 `tests/fixtures/architecture/`：

- 冷导入 `app.factory` 耗时。
- 正常和安全模式生命周期耗时；当前基线使用 `scripts/startup/performance.py` 的 no-op 组件采样，明确不启动真实插件、网络或用户数据库。
- 隔离采样的线程数、后台任务数和数据库连接数范围；真实生产连接数由部署监控另行采集。
- 架构模块数、边数、自有 SCC、目标禁止边数量。
- 目标文件行数、方法最大行数、出度。

默认不要求每项立即变小，但不得无解释显著恶化。启动和请求关键路径超过 10% 的回归必须调查。

当前可复现命令：

```bash
./.venv/bin/python scripts/startup/performance.py --repeat 3
./.venv/bin/python scripts/architecture/baseline.py --check --plugin-repo ../MoviePilot-Plugins
```

### 11.4 2026-08-17 当前验证快照

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 后端完整门禁 | `./.venv/bin/python tests/run.py` | 4,890 passed，3 skipped，0 failed |
| 架构与插件快照 | `./.venv/bin/python scripts/architecture/baseline.py --check --plugin-repo ../MoviePilot-Plugins` | 通过，无基线漂移 |
| 前端联邦 API 客户端 | `yarn test:run src/api/__tests__/client.spec.ts src/api/__tests__/index.spec.ts` | 36 passed |
| 前端类型检查 | `yarn typecheck` | 通过 |
| V3 插件契约与版本门禁 | `../MoviePilot/.venv/bin/python -m pytest tests/ci/test_v3_contract.py tests/ci/test_plugin_release_gate.py -q` | 16 passed |
| 本次 IMDb/TVDB 插件适配 | `../MoviePilot/.venv/bin/python -m pytest tests/v3/imdbsource tests/v3/tvdbdiscover -q` | 14 passed |

独立插件仓 `tests/v3` 全量当前为 62 passed、9 failed。失败集中在本次未修改的 AnimeUpscale 版本断言、LibraryScraper 未知媒体源处理、历史身份迁移和媒体服务器身份测试；它们不经过本次 IMDb/TVDB 响应适配路径，但仍是插件仓自身需要单独清理的红色基线。不得把“本次适配专项通过”扩大表述为“插件仓全量通过”。

## 12. 量化治理目标

### 12.1 短期目标（阶段 0-2）

- 动态插件 API 返回契约明确并有真实请求测试。
- `run_module` 方法名和插件 hook 100% 进入契约快照。
- 自有 SCC 不增长，消除 `_music`/`subscribe`、schemas、DB 根回流等首批环。
- Adapter→DB、Runtime→DB 新增裸依赖为零；API 既有 42 条 Model、15 条 Session 边保留在趋势基线中，新增端点不得再增加。
- 生命周期组件和 Event resolver 命中可观测。

### 12.2 中期目标（阶段 3-5）

- 本轮纳入阶段 3 的写端点不再直接持有数据库事务；其余 15 条 endpoint→Session 基线按后续切片继续收敛。
- PluginManager 不直接做市场、pip、压缩包和备份实现。
- 外部 Adapter 不导入 Oper。
- 重点 Chain 每个完成至少 3 个垂直切片迁移。
- `ChainBase` 调度可脱离真实 runtime 单测。

### 12.3 长期目标（阶段 6-7）

- 除明确第三方局部豁免外，自有 Python 模块 SCC 归零。
- `app.agent.tools.factory` 出度从约 99 降至不高于 20。
- 新 API endpoint 原则上不超过 80 行，新 Application 用例原则上不超过 150 行。
- 新插件常用能力只依赖 `app.sdk`/Host SPI；旧插件仍可运行。
- 兼容面有版本、命中数据、替代入口和机器可读清单。

这些是治理指标，不是为了达标而机械切文件。任何指标变化都要结合职责是否真正单一判断。

## 13. 风险清单与回滚策略

| 风险 | 典型触发 | 防护 | 回滚 |
| --- | --- | --- | --- |
| 插件模块结果变化 | 改写 `run_module` | 契约矩阵、记录调用序列 | Facade 切回旧 dispatcher |
| 事件顺序/并发变化 | 拆 EventManager | 可控 loop/executor 测试 | 保留旧 dispatcher 注入 |
| 插件 API 被包装 | 共用主 RouteClass | 真实请求 raw 测试 | 动态路由强制 raw |
| Singleton 身份分裂 | 新旧入口各自实例化 | `is` 测试、startup provider | 旧入口转回同一 provider |
| DB 副作用提前 | 事务迁移 | commit 失败测试、after-commit | 用例切回旧端点实现 |
| 热重载残留 | 拆 PluginManager | handler/route/service 快照 | 切回旧 lifecycle Facade |
| Provider 配置失效 | 拆 LLM provider | 配置/ID 快照与真实 fixture | 保留旧 resolver |
| 启动死锁或提前 I/O | 组合根迁移 | import/startup 线程连接快照 | 单资源恢复旧 initializer |
| Pickle/反射路径变化 | 文件改包/类移动 | `__module__`/反序列化测试 | 旧类留在原模块作门面 |
| 缓存不一致 | 调用层迁移 | key/TTL/失效时序测试 | Facade 继续使用旧缓存策略 |

架构改造的回滚单位必须是“旧 Facade 的委托切换”，不能依赖回滚数据库迁移或清理用户数据。

## 14. 明确禁止的重构方式

1. 把大文件机械切成多个互相任意导入的小文件。
2. 用函数内导入、`TYPE_CHECKING` 或字符串模块名掩盖真实运行依赖，并把它当作完成。
3. 新建另一个全局 Service Locator 取代 Singleton。
4. 为追求纯层级而复制相同 DTO、枚举和媒体身份规则。
5. 一次性重写 Chain、PluginManager、EventManager 或 Agent orchestrator。
6. 在同一批次同时移动类、改参数、改返回、改异常和改缓存。
7. 删除旧导入后批量修改官方插件来“证明兼容”。
8. 以 `app/plugins` 当前副本扫描结果代替独立插件生态审计。
9. 把主 API 的 `{success, message, data}` 信封强加给动态插件 API。
10. 以 build/pytest 通过代替依赖图、ABI 和启动副作用验证。
11. 用 LOC 作为唯一目标，导致职责更分散但依赖没有变少。
12. 架构批次夹带数据库 schema、前端协议或资源文件变更。

## 15. 完成定义

单个治理任务只有同时满足以下条件才算完成：

1. 目标职责有明确所有者和 canonical 路径。
2. 旧公开入口按兼容要求保留。
3. 宿主内部调用已经切到正确入口，不继续扩大旧模式。
4. 静态依赖方向改善，有前后数据。
5. 行为、错误、同步/异步和生命周期测试通过。
6. 插件导入、hook、对象身份和动态 API 相关测试通过。
7. 没有把问题转移成新的延迟导入、全局容器或无边界 Helper。
8. 相关架构规则、compat manifest、SDK 清单和文档已同步。
9. 变更范围可独立回滚，不依赖数据降级。
10. 汇报中明确区分已验证、未验证和剩余风险。

## 16. 后续文档维护

- 每完成一个阶段，在本文对应工作项后记录实际提交、指标变化和剩余例外。
- 若目标目录与 `docs/rules/05-architecture.md` 冲突，以更新后的正式规则为准，并在同一提交同步本文。
- 新增兼容入口必须更新 SDK/compat 机器清单，不只更新文字。
- 新发现的越层依赖先进入基线并给出清理阶段，不能用永久全局豁免消音。
- 本文不记录 `app/plugins/` 副本内容；插件生态数据应以独立插件仓的可重复扫描为准。

---

下一轮建议从阶段 6 开始，优先顺序为：**Application→Agent 的 3 条反向边 → Agent LLM/policy 自有 SCC → 消息与媒体服务器模块 SPI → 剩余 API/Session/Model 垂直切片**。每批仍按“契约快照、提取、旧入口委托、独立插件仓扫描、完整门禁”的顺序实施，不能因为阶段 0-5 已完成中期验收就删除 V3 兼容入口。
