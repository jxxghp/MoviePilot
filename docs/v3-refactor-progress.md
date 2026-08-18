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

反向边 `modules → application` 由 35 条降至 12 条（29 文件 → 8 文件）。

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
- 能力端口 **62/64 处**完成迁移：广播 7、多播 7、单播 50。
  保留 2 处：`obtain_images` 族是真管道语义（TMDB→fanart→douban
  逐级富化同一对象），三级分发无法表达。
- `run_module` 保留，服务插件生态与上述管道端口。

### 1.3 上帝基类拆解

取证：34 个 `ChainBase` 子类中 **23 个不使用任何能力端口**，
无一使用超过三个业务域——继承下发纯属负担。

- 52 个端口按七域外迁 `app/chain/ports/`：元数据、搜索、下载器、
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
取代结构上不完备的禁止前缀黑名单；未清偿的方向进**显式负债清单**，
每条附清偿方向，边消失后条目可直接删除。

---

## 二、待办

按价值与风险排序：

1. **`modules → application` 剩余 12 条**：属"配置查询"与"宿主服务"两类。
   前者做成窄协议 + 组合根注入；后者（消息通知）改为模块发事件、应用层订阅。
2. **`SecurityUtils` 整体迁入 `adapters/network/`**：需同步约 25 处测试
   patch 字符串与 SDK 导出，单独一笔提交。
3. **`RuleParser` / `BUILTIN_RULE_SET` 下沉**：阻碍是 rust 加速调用需先
   收敛为可注入的解析后端。
4. **`chain` 与 `application` 合并为统一服务层**：端口外迁后两者职责已不重叠，
   合并主要是命名与目录收敛，收益中等而改动面极大，建议在其余项清偿后评估。
5. **`_ModuleBase` 与 `_PluginBase` 统一为单一 Extension 契约**：
   目标是"内建与三方只有发行方式之别"，需先完成 1–3 项。
6. **SDK 由 compat manifest 生成**：消除"文档推 SDK、运行时绕过 SDK"的矛盾。

---

## 三、验证基线

| 阶段 | 结果 |
|---|---|
| 官方 v3 基线 | 4904 passed / 1 failed |
| 当前 | **4929 passed / 1 failed** |

唯一失败 `test_legacy_plugin_resource_imports.py::test_scanner_invalidates_equal_size_source_with_preserved_mtime`
在基线上同样失败：容器文件系统 `st_ctime_ns` 无纳秒级精度，
扫描器缓存键无法区分"同尺寸且保留 mtime"的改动，与本重构无关。

架构基线快照（`tests/fixtures/architecture/*.json`）与 `app/schemas/exports.py`
在每轮改动后由 `scripts/architecture/baseline.py --write` 与
`scripts/schema/exports.py --write` 统一刷新。
