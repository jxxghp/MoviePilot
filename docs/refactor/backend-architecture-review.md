# MoviePilot V3 后端架构收敛台账

> 更新时间：2026-08-26
>
> 本文是本轮工作的实时计划、进度和证据事实源。历史评审文档及本文在旧提交中的内容
> 只用于追溯，不再作为待办清单。架构规范仍以 `docs/rules/05-architecture.md` 为准，
> 后台动作可靠性分级以 `docs/adr/0007-background-action-reliability.md` 为准。

## 一、范围和完成定义

本轮只修改 `MoviePilot` 主仓。`MoviePilot-Frontend`、`MoviePilot-Plugins`、
`MoviePilot-Rust`、资源、构建、服务、OCR、Wiki 和官网仓仅用于核对外部契约，
不在本轮产生代码、提交或推送。

本轮不是新一轮开放式架构改造。一个事项只有同时满足以下条件才可准入：

1. 本轮开始前已有代码、提交、任务记录或“已实施但未验收/未交付”的明确证据；
2. 当前 `v3` 仍能复现未闭环状态；
3. 可以在两天内完成实现、回归、独立提交和推送；
4. 不破坏插件 ABI、跨仓契约、显式 Session/UoW 和现有并发工作；
5. 有确定的验收命令和停止条件。

仅有历史建议、尚未启动的愿望项、需要产品决策或无法在两天内完整交付的主题，
一律记录为“排除”，不创建执行目标。

每个批次保持单一主题。批次通过验证后使用显式路径暂存，提交并推送到 `v3`，
随后在本文记录提交 SHA、远端 SHA、ahead/behind 和验证结果。共享工作区中，
审计可以并行，但同时只允许一个叶子目标编辑主仓。

## 二、当前架构事实

### 2.1 宿主边界

* FastAPI 入口和启动生命周期位于 `app/startup/`，组合根负责装配运行时服务；
  API、Command 和 Application 层负责用例边界。
* Domain 保持业务规则，Application 负责用例编排，Adapter/Module 负责外部系统，
  Chain 是保留兼容面的宿主编排层。当前依赖门禁持续检查这些方向。
* 宿主 Model/Base/Oper 不隐式创建 Session；Application Command/UoW 拥有
  commit/rollback 和提交后副作用。transaction debt 基线当前为零。
* 宿主配置读取已经迁入类型化读取端口、配置服务或快照；旧 Settings 门面只为插件
  兼容保留。配置债务门禁当前通过。

### 2.2 运行时所有权

* 进程级资源由生命周期组件和显式 owner 负责启动、停止与超时收敛；
  TaskRegistry、工作流执行 owner、线程池 owner 和持久事件 Outbox 已有独立门禁。
* E0-E3 后台动作按 ADR 分级。业务事务提交成功后，非关键通知失败不能反向回滚业务；
  需要可靠交付的意图进入持久队列并由 dispatcher 重试。
* 全局兼容对象仍可能存在，但 canonical 宿主调用必须经过组合根、运行时端口或 getter，
  不得恢复 service locator 和隐式事务所有权。

### 2.3 插件和外部契约

本轮只读核对确认以下契约必须保持：

* `_PluginBase` 生命周期、`close -> stop_service` 收敛语义和 `get_*` 投影 hook；
* 旧导入路径及对象 identity、插件无 Session 的 Oper 调用兼容；
* `media_source + media_id` 成对身份、虚拟实例的 source plugin 身份；
* 动态插件 API 原始响应、插件远程组件和市场/安装边界；
* Rust 导入名、V3 资源落点、Server/OCR HTTP 边界。

官方插件基线和 483 个插件侧测试已经只读通过；其他仓没有产生修改。

## 三、历史任务对账

| 既有任务 | 当前证据 | 结论 |
|---|---|---|
| `core/helper/utils` 迁移与精确兼容导入 | 物理旧根已清零；Compat/SDK、官方插件基线和兼容测试存在 | 关闭：后续提交已完成 |
| 显式 Session/UoW 与宿主事务所有权 | transaction baseline 中隐式事务和宿主自建 Session 均为零 | 关闭：已完成 |
| 长期整改阶段 0-67 | 阶段 67 提交 `ffe6da213` 是当前 `v3` 祖先 | 阶段已交付，只复核明确余项 |
| 宿主 Settings/全局配置迁移 | `9dbe424c3` 清除 canonical 宿主兼容代理使用，门禁通过 | 关闭：已完成 |
| durable 事件可靠性第一批 | `362f60675`、`bd0795082` 已交付事件保留、清理和门禁 | 事件切片关闭；后台 owner 只审计原阶段余项 |
| 跨线程任务终态和模块关闭所有权 | `6c334f7b9`、`be7dfd77a` 已在当前分支 | 关闭：已完成 |
| Ruff/Mypy/Coverage 增量门禁第一批 | 脚本和 CI 已接入，但低水位没有真实固化 | 准入：见批次 1、2 |
| 大型 Chain 拆分、全量 sync/async 合并、扩大类型范围 | 历史内容只有建议，没有未交付实现 | 排除：未启动，不新开大任务 |
| 资源原子更新、Server helper 再拆分、Wiki/Web 陈旧内容 | 本轮只读审计新发现或属于其他仓 | 排除：超出既有主仓任务范围 |

审计开始时存在来自并行 Issue #6468 的 6 个未提交文件，该任务已独立提交推送为
`88703d645`。随后 `0a5b6a637`（Issue #6464）和 `d24c52ea9`（Issue #6472）
由其他任务合入 `origin/v3`；本轮只快进同步，没有把这些改动纳入自己的提交。

## 四、批次计划和实时进度

| 批次 | 叶子目标 | 状态 | 当前证据/停止条件 |
|---|---|---|---|
| 0 | 历史任务清账、现行架构图、外部契约核对和宿主基线对齐 | 已推送 | `d234c7132`；远端同 SHA；ahead/behind `0/0`；架构契约 `71 passed` |
| 1 | Mypy fail-closed，并把 Ruff/Mypy 已下降债务固化为真实低水位 | 已本地验证 | Mypy 完整低水位 11994、Ruff 976；专项 `33 passed`；架构契约 `71 passed`；Pylint `10.00/10` |
| 2 | 用全量串行测试初始化非零 Coverage 低水位，并补齐 CI/文档防回退契约 | 待批次 1 | fixture 当前 Application/Domain 均为 0%；最近 CI 仅作参考，必须在最终代码快照本地重建 |
| 3 | 收口阶段 62 遗留的 QQ Gateway heartbeat Timer 所有权 | 待批次 2 | Timer 只 cancel 不 join，Gateway 主线程可能在 heartbeat 仍执行时报告停止成功 |
| Final | 全仓回归、插件兼容复核、台账定稿和远端一致性验证 | 待前置批次 | 所有准入项已推送；全量测试和适用门禁通过；本地/远端 0/0 |

### 批次 0：审计与基线对齐

已完成：

* 核对主仓、历史治理 worktree、阶段 67 祖先关系和后续治理提交；
* 并行核对插件/前端、Rust/资源/外围服务契约，所有参考仓保持只读；
* 运行 transaction、configuration、complexity、async blocking、task ownership、
  service locator 和 startup performance 快速门禁，已检查项通过；
* 在同步最新远端后定位 dependency baseline 的唯一漂移：
  `app.api.endpoints.plugin` 新增到 `app.application.plugin.identity` 和
  `app.application.plugin.inventory` 的两条依赖。这是 API 调用 Application 的合法方向，
  来自已合入的 #6472，已机械更新基线；
* #6472 新增公开枚举 `PluginSourceBindingStatus` 后没有刷新 Schema 惰性导出清单，
  已用生成脚本补入并恢复确定性排序；
* `baseline.py --check-host`、`scripts/schema/exports.py --check` 和相关架构契约测试通过，
  测试结果为 `71 passed`。

交付证据：提交 `d234c7132` 已推送到 `origin/v3`；`git ls-remote` 返回同一 SHA，
`HEAD...origin/v3` 为 `0/0`，且本地 HEAD 是远端祖先。

### 批次 1：Ruff/Mypy 低水位闭环

已确认的既有缺口：

* `mypy_ratchet.py` 只解析 stdout，忽略 return code 和 stderr；当前 Mypy 内部错误退出 2
  仍被报告为“通过”，现有 Mypy fixture 因扫描中断而无效；
* 当前 Ruff 为 978 项，fixture 仍保留 1623 项，允许已消除的 645 项重新引入；
* CI 已调用两条棘轮，但缺少工具异常 fail-closed 和低水位新鲜度契约。

停止条件：等价改写触发 Mypy 崩溃的表达式；工具退出 2 或输出不可解析时门禁失败；
完整扫描正常退出并生成真实 Mypy fixture；Ruff/Mypy fixture 与当前结果完全相等；
针对性测试、两条门禁和适用静态检查通过；批次独立提交推送。

本地验收结果：

* 消息 executor 提交改为零参数 `partial`，保持空底层上下文提交和 worker 请求快照，
  Mypy 1.18.2 不再 INTERNAL ERROR；同步 fake loop 已验证提交阶段无关联 ID、渠道回调可见 ID；
* Mypy runner 固定使用 CI 的 Linux/Python 3.14 分析目标，只接受退出码 0/1，拒绝 stderr、
  缺失/重复摘要和摘要计数不一致；完整扫描覆盖 601 个文件、11994 项。旧 fixture 的
  342 个文件、6209 项已确认是截断快照；macOS 与 Linux 目标生成结果逐字一致；
* Ruff/Mypy 将增长与低水位滞后分开；已有基线存在增长时 `--write` 被拒绝，
  只有纯下降或缺失的新基线可以写入；
* Ruff fixture 从 1623 收紧到 976，Mypy 和 Ruff 默认路径复跑均通过；
* 质量/CI/上下文/严格类型专项 `33 passed`，架构契约 `71 passed`，改动文件 Pylint `10.00/10`。

待完成：提交推送并记录远端证据。

### 批次 2：Coverage 低水位闭环

已确认的既有缺口：Coverage 脚本和 CI 已落地，但 fixture 的 Application/Domain 都是
`0 statements / 0 covered / 0.00%`，所以门禁没有保护作用；开发文档仍误称该任务只手工运行。

停止条件：在批次 1 的最终代码快照上运行串行全量 Coverage；fixture 为真实非零结果；
零 statements、工具失败和低水位未固化均会失败；CI 命令存在性有契约测试；文档与 CI 一致；
批次独立提交推送。

### 批次 3：QQ Gateway heartbeat owner

阶段 62 的既有合同要求消息渠道只有在线程真实终止后才能返回成功，超时时 owner 和句柄
继续保留供重试。当前 QQ Gateway 的 Hello 回调会递归创建 `threading.Timer` 心跳；
WebSocket 和 Gateway 退出路径只调用 `cancel()`，而 `QQBot.stop()` 只等待 Gateway 主线程。
当 heartbeat callback 已进入 `send()` 时，`cancel()` 不能终止它，外层因此可能误报收敛。

停止条件：Gateway 最终退出路径在现有外层 20 秒硬预算内等待当前 Timer 真正终止；
Timer 未终止时 Gateway 线程保持存活，`QQBot` 保留 owner 并允许再次停止；故障注入证明首次
停止返回 `False`、释放 callback 后第二次返回 `True`；生命周期专项和 task ownership 门禁通过；
批次独立提交推送。

## 五、验证矩阵

每个批次按改动范围选择下列命令，Final 全部执行：

```bash
uv lock --check
uv run --locked --no-sync python scripts/architecture/baseline.py --check-host
uv run --locked --no-sync python scripts/architecture/ruff_ratchet.py
uv run --locked --no-sync python scripts/architecture/mypy_ratchet.py
uv run --locked --no-sync pytest -q tests/test_architecture_contract_baseline.py
uv run --locked --no-sync pytest -q tests/test_quality_ratchets.py tests/test_mypy_gate.py
uv run --locked --no-sync python scripts/architecture/baseline.py \
  --check-plugins --plugin-repo ../MoviePilot-Plugins
uv run --locked --no-sync python -m coverage run tests/run.py --serial
```

提交推送后还必须验证：

```bash
git ls-remote origin refs/heads/v3
git merge-base --is-ancestor HEAD origin/v3
git rev-list --left-right --count HEAD...origin/v3
```

## 六、进度日志

| 时间 | 事件 | 结果 |
|---|---|---|
| 2026-08-26 | 建立父目标和并行只读审计 | 只准入既有、可复现、两天内可交付事项 |
| 2026-08-26 | 对账阶段 0-67 和后续远端提交 | 多数旧台账余项已由后续提交关闭 |
| 2026-08-26 | 插件/外部仓契约核对 | 参考仓只读；插件基线和 483 个测试通过 |
| 2026-08-26 | 质量门禁核验 | 确认 Mypy fail-open、Ruff 低水位滞后和 Coverage 零基线 |
| 2026-08-26 | 同步并行 Issue 提交 | 主仓快进到 `d24c52ea9`，本轮文档改动完整保留 |
| 2026-08-26 | 复验宿主基线 | 只剩 #6472 引入的两条合法依赖尚未写入 fixture |
| 2026-08-26 | 收口 #6472 生成契约 | dependency fixture 与 Schema 导出清单已更新；架构契约 `71 passed` |
| 2026-08-26 | 后台 owner 历史余项审计 | 仅 QQ heartbeat Timer 满足阶段 62 既有合同和两天准入条件，其余候选关闭或排除 |
| 2026-08-26 | 批次 0 交付 | `d234c7132` 已推送；远端同 SHA；ahead/behind `0/0` |
| 2026-08-26 | 批次 1 本地验收 | Mypy 11994、Ruff 976；专项 `33 passed`；架构契约 `71 passed`；Pylint `10.00/10` |

## 七、本轮停止条件

本轮在以下条件全部满足后结束：

* 历史审计确认的所有准入项均已实现、验证、独立提交并推送；
* 其余旧事项都有“已由后续提交完成”“从未开工”“外部决策”或“非主仓”的证据；
* 宿主架构、质量、全量测试和官方插件兼容门禁通过；
* 本地提交是远端 `v3` 的祖先，ahead/behind 为 `0/0`；
* 工作区不存在本轮遗留，其他任务和其他仓的改动未被暂存、改写或提交。
