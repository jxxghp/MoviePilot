# v3 架构重构进度

> 分支 `refactor/v3-pure`，基线为官方 `upstream/v3` @ `4dc713f02`。
> 目标：移除 v2 架构残留，让宿主只有一套 v3 架构，不做双架构缝合。
> 诊断依据见 [`architecture-resurvey-upstream-v2v3-2026-08-17.md`](architecture-resurvey-upstream-v2v3-2026-08-17.md)。
>
> *Last Updated: 2026-08-17*

---

## 一、已完成

### 1.1 包级单向依赖（唯一硬性要求）

基线上存在 **4 对包级双向循环**，现已清零：

| 循环 | 清偿方式 |
|---|---|
| `schemas ↔ runtime` | `schemas/i18n.py` 翻译挂钩，实现由组合根注入 |
| `application ↔ agent` | 技能管理器经 `application/agent.py` 门面 provider 惰性解析 |
| `db ↔ application` | 订阅候选的行→业务对象翻译上移应用层边界适配器 |
| `adapters ↔ runtime` | 宿主环境探针下沉 `foundation/hostenv.py`，`SystemUtils` 委托 |
| `chain ↔ workflow` | 工作流服务迁入 `workflow/service.py` 并脱离基类继承 |

反向边 `modules → application` **已清零**（35 条 → 0）。扩展不再 import 应用服务实现：

- `runtime/hostport.py` 提供端口槽位；`directories` / `storages` / `naming` /
  `siteresource` / `filterrules` / `ruleexpression` 六个端口各自只声明模块
  实际调用到的方法，由组合根 `startup/hostport_initializer.py` 惰性注入实现。
- 媒体根路径推导下沉 `domain/mediapath.py`：返回问题描述而不打日志，
  日志级别交回各层调用侧决定（domain 不依赖 runtime）。
- URL 与路径安全原语迁入 `adapters/network/urlsafety.py`，
  原位置再导出以承接兼容层映射与 SDK 导出。
- 规则解析器与内置规则集下沉 `domain/filterrule.py`：领域层声明加速后端协议、
  未注入时回落纯 Python 解析，rust 实现由组合根注入（与识别加速同一装配点）。
  下沉后模块可直接依赖领域层，为绕开该阻碍而建的端口脚手架随之删除。

### 1.2 分发内核：v2 聚合 → v3 能力索引三级分发

v2 内核的问题是调用方无法表达意图：`run_module` 用一套歧义协议
（插件先行 → 首个非空 → 签名匹配则管道传递 → 列表则合并 → 否则中止）
兼顾通知、收集与仲裁三种需求，模块选择是 O(n) 全量扫描。

现状：

- `ModuleManager` 建立方法名能力索引 `providers_for`，按代际失效，
  能力从**运行期实例反射**推导（不信任枚举标签）。
- `ModuleInvocationDispatcher` 提供三级语义（含异步）：
  - **广播** 通知全体、不收答案，遍历是其固有代价，刻意不索引化；
  - **多播** 走索引收集族类内全部非空答案；
  - **单播** 与多播同一候选集，叠加短路取首个非空，无人认领返回 `None`。
- 全树 **250 处聚合调用完成迁移，仅存 2 处**（`ChainBase` 与两个 Mixin 的 64 处、
  具体链的 186 处）。分类以逐端口取证为准：单播占绝大多数（族类内单一答案）、
  多播用于原本依赖列表合并的场景（仪表板统计、媒体库壁纸、跨提供者艺人专辑）、
  广播用于只通知不取答案的场景（命令注册、定时钩子、缓存清理）。
- 保留 2 处：`obtain_images` 族是真管道语义（TMDB→fanart→douban
  逐级富化同一对象），三级分发无法表达。
- `run_module` 保留，服务插件生态与上述管道端口。
- 迁移中确认并规避的三类陷阱：
  - **提供者以空列表让出**：`[]` 不是"未认领"，单播会短路。艺人专辑与
    媒体库壁纸因此改用多播。
  - **提供者以 `False` 让出**：同样会被单播当成认领，逐个核对了返回值。
  - **缓存清理类端口**：改广播后，插件返回真值不再短路掉宿主模块的实际清理。
- 跨层穿透一并消除：推荐链不再直接驱动其它链的分发原语，改调其公开方法；
  TMDB 发现与趋势端口补齐 `raise_exception` 透传，避免异常语义在改调后丢失。

### 1.3 上帝基类拆解

取证：34 个 `ChainBase` 子类中 **23 个不使用任何能力端口**，
无一使用超过三个业务域——继承下发纯属负担。

- 52 个端口按七域外迁 `app/application/orchestration/ports/`：元数据、搜索、下载器、
  整理分类、媒体库、报文解析、系统钩子；实现只保留一份。
- `ChainBase` 保留全部同名端口作一行转发（插件契约面零变动），
  端口方法内直接调用分发原语的地方由 64 处降为 0。
- 客户端持有分发宿主而非自建调度器，服务可只组合所需域。
- `WebhookChain` 作为示范不再继承基类，只组合报文解析域。

### 1.4 编排面收敛

`SchedulerChain` / `CommandChain` 不再继承 `ChainBase`，
改为持有并按需委托消息与分发设施——调度器与命令注册表回归进程级服务。

### 1.5 宿主脱离 v2 兼容路径

宿主代码对 `app.core` / `app.helper` / `app.utils` / `app.log` 的引用**归零**
（插件基类原先就走这些虚拟路径，等于每个插件天然锚定 v2 旧世界）。
兼容层本体完整保留，`_PluginBase` 公开 API 一字未变。

### 1.6 扩展点去硬编码

| 扩展点 | 原状 | 现状 |
|---|---|---|
| Indexer Spider | 硬 import + 静态字典 + 同步/异步两条 if/elif 链，同一知识四处表达 | 单一注册表，构造参数按爬虫自身签名推导 |
| Agent 内建工具 | 82 项静态元组 + 90 行硬 import | 目录扫描发现，强断言锁定工具清单与顺序 |
| 模块 subtype | 必须是 6 个内核枚举成员，否则启动失败 | 非通知类放宽为自由字符串；通知渠道仍需登记（`Message.channel` 是类型化字段） |

### 1.7 治理换轨

`tests/test_architecture_dependencies.py` 新增**包级允许依赖矩阵**断言，
取代结构上不完备的禁止前缀黑名单。负债清单 `DEPENDENCY_DEBT` **已清空**，
矩阵成为无例外的硬约束：

- Agent 工具经 `runtime/diagnostics.py`、`runtime/workflows.py` 端口取用
  自检诊断与工作流执行，扩展之间不再互相 import。
- 认证依赖下沉 `application/security/dependencies.py`：函数名、签名、
  `Depends` 链、状态码与文案逐字保留；`api/deps.py` 再导出使端点侧零改动，
  插件兼容门面改从应用层取用，SDK 不再反向依赖入口层。

### 1.8 服务层合并

`app/application/` 成为唯一的服务层包，跨入口复用的用例编排收敛为其
`orchestration/` 子包（44 个文件），两个顶级包并列且边界含糊的状态结束。

- 896 处导入路径由脚本统一重写，覆盖 `app/` 与 `tests/` 共 207 个文件。
- `app.chain.*` 登记为兼容层旧导入根（45 条别名 + 虚拟包），
  存量插件直接 import 具体链类的写法不受影响，且旧路径与新路径解析为同一对象。
- 未选用 `app/services/` 作为包名：它是上游退役并设有防复活断言的名字。
- 门禁的编排层专属断言（不得穿透模块内部、不得依赖 Agent 实现、
  不得引入下载器 SDK）意图保留，仅同步路径。

### 1.9 单一 Extension 契约

`runtime/extensions/contract.py` 声明扩展的共同面：发行方式（预装 / 市场）与
失败归属、身份、生命周期、能力与钩子探测，以及分发用的提供者视图与来源协议。

- **两个基类源码零改动**，经适配器投影成契约视图：模块侧映射
  `init_module`/`stop`/`test`，插件侧映射 `init_plugin`/`stop_service`/`get_state`。
  协议声明语义、名称映射留在适配层——两者的打包模型本就不同
  （包级单例 + 配置开关 vs 多实例 + 安装清单），合并基类会破坏插件生态。
- 分发内核只消费提供者视图，不再假设扩展的具体形状：错误按失败归属分流、
  签名接力与逐调用日志按视图声明。六个成对方法合并为四个通用实现。
- 能力索引、插件投影与插件生命周期改经契约探测；重复的钩子与能力探测合并为一份。
- **验收自评**：新增一种发行方式无需改动分发内核、能力索引与投影，
  已由一个只实现协议的第三方来源接入四级分发验证；组合根仍需一行装配。

已知限制：插件生态没有连通性自检契约，故插件视图的自检恒为空
（把它映射到启用态会把"已启用"误报成"可连通"）；能力索引仍返回模块实例
而非视图，因为编排层与既有测试依赖实例身份。

---

## 二、待办

1. **SDK 由 compat manifest 生成**：消除"文档推 SDK、运行时绕过 SDK"的矛盾
   （SDK 目前在宿主生产代码中仍是零消费者）。

---

## 三、验证基线

| 阶段 | 结果 |
|---|---|
| 官方 v3 基线 | 4904 passed / 1 failed |
| 当前 | **4979 passed / 0 failed** |

官方基线上那条失败是
`test_legacy_plugin_resource_imports.py::test_scanner_invalidates_equal_size_source_with_preserved_mtime`：
容器文件系统 `st_ctime_ns` 无纳秒级精度，扫描器缓存键无法区分
"同尺寸且保留 mtime"的改动，与本重构无关，在负载与时序变化下时通时不通。

架构基线快照（`tests/fixtures/architecture/*.json`）与 `app/schemas/exports.py`
在每轮改动后由 `scripts/architecture/baseline.py --write` 与
`scripts/schema/exports.py --write` 统一刷新。
