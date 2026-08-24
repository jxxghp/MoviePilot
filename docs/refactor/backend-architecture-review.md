# 后端架构优化点评审（2026-08）

> 本文档是 2026-08-24 基于当前 `v3` 分支代码的全面架构评审结论，取代此前
> `docs/refactor/` 下的分阶段治理文档（`backend-architecture-governance.md`、
> `backend-module-refactor-compatibility.md`、`backend-architecture-next-stage.md`、
> `module-quality-scale.md`，历史内容见对应提交的 git 记录）。分层权威规范仍以
> [`docs/rules/05-architecture.md`](../rules/05-architecture.md) 为准，后台动作可靠性分级
> （E0–E3）的决策依据见 [`docs/adr/0007-background-action-reliability.md`](../adr/0007-background-action-reliability.md)。
> 本文档不重复既有约束，只记录现状差距与改进方向。

## 总体结论

v3 重构骨架健康：架构测试 `tests/test_architecture_dependencies.py` 全部通过，
chain 层零 `app.db` / `app.modules` 内部直连，domain 与 chain 层配置注入纪律近乎完全落实。
当前的主要优化空间不是"分层错误"，而是三类问题：

1. **上帝类**——领域算法埋在编排链里；
2. **迁移过半的全局状态**——注入通道已建好，存量未迁完；
3. **收缩的质量门禁**——静态检查名义严格、实际覆盖面很小。

## 一、Chain 层上帝类：领域算法埋在编排类里（优先级最高）

| 文件 | 行数 | 问题 |
|---|---|---|
| `app/chain/subscribe.py` | 4155 | 约 15 个职责域：洗版优先级纯计算约 650 行（L201-847）、订阅创建状态机（L976-1406）、搜索编排（单方法 285 行 L1505-1790）、匹配编排（L2032-2421）、进度/完成事实管理（L2735-3106）、分享跟随、日历缓存、远程交互删除等 |
| `app/chain/download.py` | 2293 | `_execute_batch_download`（L1533-2052）是"电影→整季→按集→拆包"四轮择优策略引擎，本质是领域算法却埋在链里；字幕下载子系统约 500 行；失败冷却指纹（L774-946） |
| `app/chain/transfer.py` | 3208 | 线程池基础设施 + 内存队列/落盘回放 + 整理编排三类职责同居一个单例；MRO 深达 10 个类；`_execute_transfer` 单方法约 820 行 |
| `app/chain/media.py` | 2060 | 音乐识别子域（约 900 行路径专辑推断）与影视识别门面强行同居一个类 |

**建议**：按项目已有的 topic-package 先例（参照 `application/subscription/` 的拆分方式），
把纯策略计算下沉到 `domain` 或 `application`：

* 洗版优先级/缺集计算 → 如 `application/subscription/priority.py` 或 domain；
* 批量择优下载策略 → 如 `application/download/strategy.py`；
* 音乐识别 → 并入既有 `application/music/` 目录；
* 链本身只保留编排职责。

## 二、sync/async 手工双写造成系统性重复

* `chain/media.py` 有 **11 对**同步/异步孪生方法；
  `_recognize_with_fallback_by_meta`（L647-723）与其异步版（L1596-1669）结构逐行对应。
* `chain/search.py` 的 `process / async_process / async_process_stream` 三份实现
  （L1665/1758/1848），站点并发搜索三份（L2301/2486/2602），字幕搜索再两份。
* 过滤规则组解析同一表达式出现 **5 处**；`_media_recognize_kwargs` 三胞胎逐行相同；
  "未识别到媒体信息"告警模式全目录 **18 处**。
* 死代码残留：`transfer.py` L2146-2173 变量初始化两次；`search.py` L2366-2378
  if/else 两分支提交完全相同的调用。

**建议**：统一封装"同步包装异步"的基础设施（复用 `app/runtime/execution.py` 的跨线程提交边界），
新代码只写 async 版本；重复告警/解析模式收敛到共享 mixin 或 application 服务。

## 三、Mixin 组合缺契约 + 两处 dispatch 绕过

* mixin 大量使用 `self.messageoper` / `self.run_module` 等，但无 Protocol/ABC 声明依赖，
  靠 docstring 书面承认（`_music.py:42-46`）；每个链无差别继承全部基础能力，
  TransferChain MRO 达 10 个类。
* `_music.py:9-11`、`_transfer.py:22-24` mixin 反向 import 具体链，形成耦合网。
* 正面样板是 `_interaction.py` 的 `InteractionChainMixin`（显式 `_interaction_handler_type`
  注入点 + 抽象方法），值得推广为所有 mixin 的标准姿势。
* 违反"chains reach modules only through run_module dispatch"约束的两处实锤：
  * `media.py:626-638` 硬编码 `get_running_module("TheMovieDbModule")` 直调其方法；
  * `scraping.py:584-598` 自行聚合 `metadata_img` 多模块结果
    （dispatcher 已有 aggregation contract 可表达）。

## 四、全局状态："通道已建、存量过半"

| 全局点 | 现状 | 建议 |
|---|---|---|
| `eventmanager` 单例 | 36 个文件直连 import；其中 chain 层 8 个文件绕过已注入的 `context.event_manager`（search/download/media/subscribe/transfer/site/scraping/workflow） | chain 层直连改为使用注入上下文，改动机械、风险低 |
| `global_vars` 容器 | 47 个文件 147 处引用，workflow + chain 占近半，被当"停止信号总线"广泛直读 | 停止信号演进为 `runtime/state.py` 的显式契约 |
| `RuntimeSettingsCompat` | 119 个文件 import（modules 占 60），形式上是端口、用法上仍是每模块全局对象 | modules 层逐步改为注入快照 |
| Singleton 元类 | 41 处 class 使用，与 getter 门面双轨并存 | 维持双轨兼容，新增能力一律走 getter 门面 |

## 五、启动生命周期欠账（违反自家规则）

规则明确"新增进程级资源不得只在 lifespan() 中追加过程代码"，但 `startup/lifecycle/__init__.py`
仍有 4 处过程式资源管理：

1. 主事件循环注册/清理（global_vars set_loop/clear_loop）未进组件清单；
2. 插件同步与启动收尾任务（init_extra）游离在清单之外，不参与依赖排序和清理集合推导；
3. 停止标志设置；
4. 日志关闭靠注释约定顺序。

另有两个组合根脆弱点：

* `initializers/command.py`、`initializers/scheduler.py`、`initializers/agent.py`
  在 **import 时即注册**全局服务，构成隐式时序契约，任何提前 import 都会改变注册顺序；
* `initializers/modules.py`（908 行、约 68 处 configure 调用）事实上成为第二过程式组合根，
  与 `composition/` 的纯函数式装配存在职责重叠。

## 六、质量门禁名义严格、实际收缩（投入产出比最高）

| 工具 | 现状 | 建议 |
|---|---|---|
| mypy | `strict=True` 但 `files=` 白名单仅 41 个文件；全量扫描约 10072 个错误被白名单挡在门外；`follow_imports = skip` 架空跨模块检查 | 引入棘轮机制：新增文件必须达标，白名单只减不增 |
| ruff | 完全不在工具链中 | 引入并启用 import 排序等检查，与架构测试互补 |
| pylint | `disable=all` 后仅 enable 12 项高确定性检查 | 维持现状可接受，不指望它兜底 |
| 覆盖率 | 无 `fail_under` 门禁，coverage job 仅手动触发 | 至少给 `app/application`、`app/domain` 设阈值 |

## 七、测试隔离的风险点

四道防线（CONFIG_DIR 隔离、网络守卫双重断言、水位回收、会话收尾）设计精细，但有三个隐患：

* 单一 SQLite 库按主键水位回收，正确性依赖每个用例自觉登记模型表，漏登记即污染后续用例，
  且清理失败被静默吞掉；
* 约 290 行巨型 autouse fixture 装配几十个进程级服务槽位，teardown 只复位
  `reset_plugin_system()` 一个，其余槽位跨用例残留（164 处手动 `reset_*` 说明恢复靠约定而非机制）;
* 54 个 `unittest.TestCase` 残留文件（占测试文件的 9.9%），按 AGENTS.md 策略在触碰时机会性转换。

## 八、其他次要点

* **非单例链反复实例化**：`MediaChain()` 全仓构造 54 处，`DownloadChain().batch_download()`
  在订阅循环内反复构造重跑 init；建议统一走 getter 门面。
* `app/scheduler.py`（2096 行）：入边已收敛到组合根，但调度器 + GC + 壁纸 + 媒体库同步等
  job 实现混在一个文件，建议按 job 域拆分。
* `app/agent/orchestrator.py`（3655 行）：`MoviePilotAgent` 与 `AgentManager` 同居，
  含会话快照、任务队列、脱敏确认等多个内部类，可按 `agent/` 已有子包惯例拆分。

## 建议实施顺序

1. **补质量门禁棘轮**（mypy/ruff/覆盖率）——先止血，防止新增债务；
2. **完成 chain 层 eventmanager/global_vars 存量迁移**——机械性工作，风险低；
3. **按 domain 下沉方式拆 subscribe/download 的领域算法**——收益最大；
4. **修两处 dispatch 绕过与 lifespan 清单欠账**——对齐自家规则。

## 附：评审方法

基于 2026-08-24 对 `v3` 分支的证据收集：架构测试运行结果、全仓静态扫描
（mypy 全量 vs 白名单对比）、大文件方法级职责盘点（wc/rg/class 清单）、
全局符号引用统计（eventmanager/global_vars/settings import 分布）、
lifecycle 组件清单与 lifespan 过程式代码比对、tests/conftest 隔离机制审查。
