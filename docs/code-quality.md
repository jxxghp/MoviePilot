# 代码门禁：结构指标与扩围建议

2026-09-30 对 `v3@b4175a4f88421cf3515eeb651b8a4febe836642f` 的门禁进行审计，并在其上调整
复杂度与严格类型检查。提交前衔接到最新 `v3@4a2ef7570659d587c2ebbddf02177443237734dc`，
初始结构基线按该版本重新采集；宿主业务源码与该版本完全一致。以下区分已落地规则与后续建议。

## 行数为什么不再阻断

旧 `complexity.py` v1 对公共 API 入口设置 80 行预算，对 Application/Chain 公共入口设置
150 行预算；v2 对 API、Application、Chain、Scheduler 的函数、类、文件设置
150/500/1000 行预算。函数和类使用 AST `end_lineno - lineno + 1`，会把 docstring、
注释、空行和参数换行计入。它衡量源码尺寸，并不衡量分支复杂度；压缩格式能够降低数值，
补充契约说明却可能触发回退。同一长函数还可能同时消耗函数、类和文件三份预算。

源码尺寸仍适合寻找职责混杂热点，但不适合作为逐行禁止增长的合并条件。拆分应以稳定职责、
可测试行为和清晰依赖为依据，不能为了满足行数而删除说明或搬运代码。

## 当前复杂度合同

`scripts/architecture/complexity.py` 使用 `uv.lock` 锁定的 Ruff，独立执行以下两条规则：

| 指标 | 新增函数上限 | 存量处理 |
| --- | --- | --- |
| C901：McCabe 圈复杂度 | 15 | 超限项按函数登记，只能保持或下降 |
| PLR1702：嵌套控制块深度 | 5 | 同函数多个告警取最大深度，只能保持或下降 |
| 方法/类/文件源码尺寸 | 无硬限制 | 超过 150/500/1000 行出现在观察报告 |

范围为所有 `app/**/*.py`，只排除 `app/plugins/**` 运行时插件副本；包含 Agent、Runtime、
Modules、Adapters、Startup、Domain、DB、Workflow、私有方法、dunder 方法和嵌套函数。
局部 Ruff 配置、`noqa` 与 `.gitignore` 不会隐藏这些指标。

基线身份使用“规则 + 相对文件路径 + 完整词法函数名”，不保存物理行号；同词法名的重复定义
按源码顺序附加序号，避免 property 或条件重定义覆盖预算。移动或重命名超限函数会成为新项，
需要在职责迁移中单独审查，不能自动挪用旧额度。不同函数的增减也不能互相抵消。

首次从行数策略迁移到结构策略的快照为 C901 138 个函数、PLR1702 39 个函数；同一函数可能
同时出现在两类中。这是本次迁移的历史证据，不要求后续数字保持不变。源码观察报告有
62 个长函数、123 个大类和 66 个大文件。没有为建立新基线修改业务源码。

当前 `tests/fixtures/architecture/complexity-baseline.json` 是指标事实源，并记录策略版本
和阈值；旧 `complexity-v2-baseline.json` 与 `--v2` 已退役。门禁行为为：

- 新增超限或既有超限增长：检查失败，`--write` 也拒绝覆盖已有基线。
- 指标降低、回到阈值内或函数删除：先提示低水位未固化，审查后 `--write` 收紧基线。
- 语法错误、工具错误、不完整输出、无法映射的诊断、缺失源码或基线：失败，不能当作零债务。
- `--report` 输出结构指标和源码尺寸。CI 上传 `complexity-report`，失败时也保留已生成报告。

```bash
uv run --locked --no-sync python scripts/architecture/complexity.py --report /tmp/moviepilot-complexity-report.json
uv run --locked --no-sync python scripts/architecture/complexity.py --write
uv run --locked --no-sync python scripts/architecture/complexity.py
```

`--write` 不是接受新债务的审批入口。首次初始化、算法升级、阈值调整和职责迁移需要审查
源码、指标 diff 和相关测试；不得删除已有基线后重建来绕过增长检查。

算法口径跟随锁定的 Ruff，未自行实现另一套复杂度定义。
[C901 的配置](https://docs.astral.sh/ruff/settings/#lint_mccabe_max-complexity) 衡量控制流分支，
不是运行时耗时、内存复杂度或完整认知复杂度。
[PLR1702](https://docs.astral.sh/ruff/rules/too-many-nested-blocks/) 仍属 preview，只有此独立
检查启用 preview；升级 Ruff 时应跑真实 Ruff 回归用例并审查指标 diff，不自动刷新基线。
短路布尔表达式、推导式、跨函数调用链和职责耦合仍需行为测试与架构审查。

## 类型检查的本次扩围

`mypy.ini` 的严格范围从 42 个文件扩大到 45 个，新增：

- `app/runtime/tasks.py`：后台任务登记、提交和关闭。
- `app/scheduler/registry.py`：调度器代际、所有权与句柄。
- `app/startup/lifecycle/components.py`：组件启动与关闭。

三个文件及完整严格清单实测通过，不增加忽略规则或类型债务。全宿主 mypy ratchet 继续保留。
严格清单当前仍采用 `follow_imports = skip` 和 `ignore_missing_imports = True`，不能等同于
传递依赖全部严格。Agent session/lifecycle 与 Scheduler lifecycle 的扩围需要一起处理父类、
回调和返回值类型；全仓错误计数为零并不保证单独纳入 strict frontier 后也为零。

## 后续补充顺序

| 优先级 | 现有缺口 | 建议落点与验收 |
| --- | --- | --- |
| P0 | GitHub `v3` 查询结果 `protected=false`，生效分支规则为空 | 增加固定名称的 CI 汇总检查，再配置 required checks；汇总要正确处理成功复用、取消和失败，不能把所有 skipped 当成功。远端规则属于仓库策略，当前改动未更改它 |
| P1 | 覆盖率只阻断 Application/Domain 的聚合行覆盖率低于 80%；虽采集分支覆盖率，但没有分支下限 | 先为 transfer、download、scheduler、tasks、Agent 生命周期等关键文件组建立 Ubuntu 全量 CI 分支基线，再加入变更行覆盖率；避免用整个包的高覆盖率掩盖关键路径。macOS 报告只作诊断 |
| P1 | strict frontier 对部分父类和传递依赖仍视为 Any | 按“父类/Port/DTO → lifecycle → 调用者”扩到 Agent session、Scheduler lifecycle、插件管理器；先消除真实类型问题，再纳入，不增添 ignore |
| P1 | 漏洞审计位于正式版/Beta 发布链路，依赖兼容 PR 工作流只做平台安装验证 | 在依赖变更 PR 对同一 locked 运行时依赖做 pip-audit；不要复制一份依赖清单，也无需每个普通源码 PR 重跑多架构镜像扫描 |
| P2 | 常规 Ruff 仅启用 E4/E7/E9/F/I | 优先试运行 B006（可变默认参数）、B023（循环变量闭包捕获）、B018（无效表达式）；审查误报后按范围纳入。审计时 app 中 B006 为 0、B023 为 5、B018 为 1，不自动把新诊断认定为已证实的 bug |
| P2 | 部分其它 ratchet 写入器仍能直接覆盖增长，Ruff/mypy 按文件计数可能发生同规则问题抵消 | 统一已有基线的写保护；对重点规则考虑稳定符号/诊断指纹。先补“无法洗入回退”的行为用例，避免只测 fixture 字符串 |

依赖方向、循环依赖、直接出站、异步阻塞、后台任务所有权、原生并发和 service locator
已经有门禁，应保留并按实际漏报补强，不重复添加同义规则。鉴权、数据持久化、幂等、取消、
崩溃恢复和插件 ABI 继续由行为/契约测试保护，源码尺寸或复杂度分值不能代替这些测试。
