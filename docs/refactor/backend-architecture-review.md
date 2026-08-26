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

## 优化点执行状态（2026-08-24）

| 优化点 | 状态 | 说明 |
|---|---|---|
| 1.1 subscribe 洗版优先级算法下沉 | ✅ 已完成 | 迁入 `application/subscription/priority.py`，链上留兼容委托 |
| 1.2 download 批量择优规则下沉 | ✅ 已完成 | 迁入 `application/download/selection.py` |
| 1.3 media 音乐识别子域拆分 | ⏸ 后续任务 | 需大范围行为等价性验证 |
| 1.4 transfer 三类职责拆分 | ⏸ 后续任务 | 线程池/队列/编排解耦需单独设计 |
| 2. 死代码清理 | ✅ 已完成 | transfer 双重初始化、search 相同分支 |
| 2. sync/async 孪生合并 | ⏸ 后续任务 | 新代码执行"只写 async"纪律 |
| 3. media.py dispatch 绕过 | ✅ 已完成 | 改按 source 路由走统一调度 |
| 3. scraping.py metadata_img 聚合 | ⏸ 需架构决策 | 须先新增"按键填充"聚合模式与 provider 排序策略 |
| 3. Mixin Protocol 契约化 | ✅ 第一批完成 | 新增 `app/chain/_contracts.py`，存量 mixin 声明宿主 Protocol 与可替换工厂接缝 |
| 4. chain 层 eventmanager 迁移 | ✅ 已完成 | 17 处实例方法改注入；装饰器/staticmethod 按设计保留 |
| 4. global_vars / settings 注入迁移 | ✅ 停止信号完成 | `StopState` 已成为停止读写入口；`global_vars` 仅保留兼容门面，settings 仍按域渐进迁移 |
| 5. lifespan 停止信号+插件收尾组件化 | ✅ 已完成 | 进入声明式清单，含快照测试 |
| 5. lifespan 主循环/日志关闭组件化 | ⏸ 需架构决策 | 引擎 FAIL_FAST break 语义需先扩展 |
| 6. mypy 错误数棘轮 | ✅ 已完成 | `scripts/architecture/mypy_ratchet.py` 接入 CI |
| 6. ruff 引入 / 覆盖率阈值 | ✅ 第一批完成 | Ruff 与覆盖率棘轮接入架构工作流，基线只允许下降；覆盖率从应用/领域包开始积累 |
| 8. 订阅循环链构造提升 | ✅ 已完成 | SearchChain 循环外复用 |
| 8. 非单例链 getter 门面统一 | ⏸ 需架构决策 | 改变链生命周期语义 |

> 测试隔离风险点（第七节）与 scheduler/orchestrator 拆分按 AGENTS.md
> "机会性转换、最小改动"原则在后续触碰相关文件时渐进处理。

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

> 处理进展（2026-08-24）：
> * 洗版优先级/缺集计算的 25 个纯函数已迁入 `app/application/subscription/priority.py`
>   （含 `prepare_subscribe_progress_fields`），链上保留单行兼容委托，新模块 mypy 零错误；
> * 批量择优的缺集记账与覆盖判定规则（9 个纯函数）已迁入
>   `app/application/download/selection.py`，`_execute_batch_download` 内嵌套闭包改为委托；
> * 音乐识别子域与 media.py 的 sync/async 孪生合并、transfer.py 的三类职责拆分
>   涉及大范围行为等价性验证，列为后续独立任务。

## 二、sync/async 手工双写造成系统性重复

* `chain/media.py` 有 **11 对**同步/异步孪生方法；
  `_recognize_with_fallback_by_meta`（L647-723）与其异步版（L1596-1669）结构逐行对应。
* `chain/search.py` 的 `process / async_process / async_process_stream` 三份实现
  （L1665/1758/1848），站点并发搜索三份（L2301/2486/2602），字幕搜索再两份。
* 过滤规则组解析同一表达式出现 **5 处**；`_media_recognize_kwargs` 三胞胎逐行相同；
  "未识别到媒体信息"告警模式全目录 **18 处**。
* 死代码残留：`transfer.py` L2146-2173 变量初始化两次；`search.py` L2366-2378
  if/else 两分支提交完全相同的调用。

> 处理进展（2026-08-24）：死代码两处已清理。sync/async 孪生合并与重复模式收敛
> 涉及大量行为等价性验证（`media.py` 11 对、`search.py` 三份站点搜索实现），
> 列为后续独立任务，优先在新代码中执行"只写 async 版本"的纪律。

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

> 处理进展（2026-08-24）：`media.py` 的 TMDB 补充已改为按 source 路由的
> `run_module("recognize_media")` 统一调度（宿主识别模块对非自身来源快速返回 None，
> 与 `_recognition.py` 既有模式一致）。`scraping.py` 的 `metadata_img` 合并经复核
> **不能直接替换**：手写循环是"按键合并、宿主模块限定"，而 dispatcher 现有聚合只有
> 整体短路或后值覆盖，且 dispatch 全局插件优先会让第三方插件图片覆盖内置源图片。
> 如需收口，须先做显式架构决策：新增"按键填充"聚合模式并提供调用方可控的
> provider 排序策略，否则维持现状是行为最安全的选择。

## 四、全局状态："通道已建、存量过半"

| 全局点 | 现状 | 建议 |
|---|---|---|
| `eventmanager` 单例 | 36 个文件直连 import；其中 chain 层 8 个文件绕过已注入的 `context.event_manager`（search/download/media/subscribe/transfer/site/scraping/workflow） | chain 层直连改为使用注入上下文，改动机械、风险低 |
| `global_vars` 容器 | 47 个文件 147 处引用，workflow + chain 占近半，被当"停止信号总线"广泛直读 | 停止信号演进为 `runtime/state.py` 的显式契约 |
| 运行时 Settings 读取 | 宿主已统一通过 `app.runtime.settings.get_runtime_setting()` 读取；可变部署配置只经 `RuntimeSettingsService` 管理，旧 `settings` 对象仅由插件兼容入口保留 | 新代码使用只读端口或类型化快照，禁止恢复模块级代理 |
| Singleton 元类 | 41 处 class 使用，与 getter 门面双轨并存 | 维持双轨兼容，新增能力一律走 getter 门面 |

> 处理进展（2026-08-26）：Agent、Module、Adapter、Doctor、Startup、CLI 及入口层已完成 Settings 读取迁移，宿主源码不再导入或实例化旧兼容代理；插件兼容入口继续提供旧 `settings` ABI。后续新增宿主代码必须依赖读取端口、配置服务或不可变快照。
>
> 处理进展（2026-08-24）：chain 层 17 处实例方法调用已改用注入的 `self.eventmanager`
> （`transfer.py`、`media.py` 已完全脱离全局导入，其余文件因 `@eventmanager.register`
> 装饰器注册与 staticmethod 调用点按设计保留全局引用）；`media.py` 的
> `select_recognize_source`/`async_select_recognize_source` 由 staticmethod 转为
> 实例方法以使用注入依赖。global_vars 停止信号总线化与 modules 层配置注入迁移
> 影响面大，列为后续独立任务。

## 五、启动生命周期欠账（违反自家规则）

规则明确"新增进程级资源不得只在 lifespan() 中追加过程代码"，但 `startup/lifecycle/__init__.py`
仍有 4 处过程式资源管理：

1. 主事件循环注册/清理（global_vars set_loop/clear_loop）未进组件清单；
2. 插件同步与启动收尾任务（init_extra）游离在清单之外，不参与依赖排序和清理集合推导；
3. 停止标志设置；
4. 日志关闭靠注释约定顺序。

> 处理进展（2026-08-24）：第 2、3 项已组件化——"停止信号"（stop_order=4，先于一切
> 资源释放发出停机通知，启动失败清理同样生效）与"插件同步与启动收尾"（start_order=150，
> 依赖工作流，取消权仍归最前置 TaskRegistry owner）均已进入声明式清单并通过顺序快照测试。
> 第 1、4 项经复核**不能直接进清单**：当前引擎的 FAIL_FAST break 会跳过更高 stop_order
> 的组件，而主循环清除与日志关闭在现有嵌套 finally 中是无条件执行的"最外层保底"，
> 直接搬移会让无关 owner 未收敛时跳过这两步（回归）。如需收口，须先做显式引擎决策：
> 引入"最终保底 finalizer"概念或调整 FAIL_FAST 传播语义。

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
  > 处理进展（2026-08-24）：订阅搜索循环内的 `SearchChain()` 已提升到循环外复用；
  > `MediaChain`/`TransferChain` 本身是 Singleton 元类（构造为缓存命中，代价低）。
  > 把 `DownloadChain`/`SearchChain`/`SubscribeChain` 统一改为进程级 getter 门面
  > 会改变链实例的生命周期与状态共享语义，需显式架构决策后另行推进。
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
