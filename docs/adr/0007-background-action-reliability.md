# ADR-0007：后台动作可靠性与完成语义

- 状态：Accepted
- 日期：2026-08-21
- 对应任务：ARCH-250

## 决策

MoviePilot 保持模块化单体，不把所有后台动作迁到分布式队列。每个动作必须在 E0–E3 中登记；
调用方只能按登记的完成点向用户宣称成功。

| 等级 | 完成点 | 恢复要求 | 失败表达 |
| --- | --- | --- | --- |
| E0 即时信号 | 已进入当前进程队列或当前 handler 返回 | 允许丢失，不跨重启恢复 | 日志/临时 UI 状态 |
| E1 可重建任务 | 已登记可由周期扫描重新生成的意图 | 幂等、有限重试、下周期可重建 | job 日志和下次运行 |
| E2 用户动作后置副作用 | 业务事务与 durable intent 同时提交 | 可重放、幂等键、有限退避、dead letter | 可查询 attempt/last_error |
| E3 数据完成状态 | 持久任务所有步骤提交并记录终态 | 崩溃恢复、步骤幂等、人工恢复入口 | 持久失败状态/补偿说明 |

`durable-required` 是目标语义，不代表当前实现已经 durable。ARCH-251 前，Event Registry 中标记该值的
事件仍应在风险报告中说明崩溃窗口。

Registry 里 `delivery=durable_required` 的是六个事件：`SubscribeAdded`、`SubscribeModified`、
`SubscribeDeleted`、`DownloadAdded`、`TransferComplete`、`TransferFailed`，广播已由业务事务内的 outbox
intent 提供 at-least-once 恢复。`SubscribeComplete` 另有 `subscribe.complete` / `subscribe.complete.report`
两条 outbox intent，但它在 Registry 里的 `delivery` 仍是 `ephemeral`。
订阅完成的历史新增、订阅删除、完成事件和完成统计 intent 同事务提交，
提交后通知/事件/统计仍按原顺序执行，事件与统计失败保持独立 pending。payload 保持插件 dict/对象 ABI，
并增加可选幂等键。下载和整理的 outbox 只保存
可 JSON 序列化的快照，重放时恢复旧对象字段。这不覆盖第三方插件自行发送的裸事件，也不代表订阅通知
和外部统计上报已经全部 durable。

## Event 映射

Event Contract Registry 是 53 个事件的逐项机器清单。下表按相同语义分组列出每个事件，不省略事件名。

### E0：进程内通知或扩展 Hook

- 插件/命令：`PluginReload`、`PluginAction`、`PluginTriggered`、`CommandExcute`。
- 站点/历史：`SiteDeleted`、`SiteUpdated`、`SiteRefreshed`、`HistoryDeleted`、
  `DownloadFileDeleted`、`DownloadDeleted`。
- 消息/UI：`UserMessage`、`WebhookMessage`、`NoticeMessage`、`MessageAction`。
- 生命周期/诊断：`SystemError`、`ModuleReload`、`ConfigChanged`、`WorkflowExecute`、
  `AgentTokensUsage`、`MetadataScrape`、`SubscribeComplete`、`SubtitleTransferComplete`、
  `SubtitleTransferFailed`、`AudioTransferComplete`、`AudioTransferFailed`。
- 链式扩展：全部 22 个 `ChainEventType`（`PluginDataReset`、`NameRecognize`、
  `MusicNameRecognize`、`MediaRecognize`、`MusicMediaRecognize`、`AuthVerification`、
  `AuthIntercept`、`CommandRegister`、`TransferRename`、`TransferRenameBuild`、
  `TransferIntercept`、`TransferOverwriteCheck`、`ResourceSelection`、`ResourceDownload`、
  `DiscoverSource`、`MediaRecognizeConvert`、`RecommendSource`、`WorkflowExecution`、
  `StorageOperSelection`、`AgentLLMProvider`、`SubscribeEpisodesRefresh`、
  `SubscribeCompletionCheck`）。这些是当前调用栈内决策/扩展，不独立恢复。

### E2：业务提交后的用户副作用

- `SubscribeAdded`、`SubscribeModified`、`SubscribeDeleted`：订阅业务行 commit 是业务完成点；事件、
  通知和服务端上报必须由同事务 durable intent 驱动。ARCH-251 首选 `SubscribeAdded` pilot。
- `DownloadAdded`：下载提交成功后，历史/通知不得仅依赖进程内回调；后续独立 pilot。
- `TransferComplete`、`TransferFailed`：整理步骤本身属于 E3，但向事件消费者发布结果属于 E2。

## 非 Event 后台机制映射

### FastAPI BackgroundTasks

- 订阅手工搜索调度、插件市场刷新、低价值上报：E1；响应成功只表示已接受本进程调度，不表示执行完成。
- 若任务源于已提交的用户数据且不可从数据库重建，必须提升为 E2，不得继续新增裸 BackgroundTasks。

### Scheduler jobs

- 站点数据、缓存、市场、CookieCloud、媒体服务器周期同步、垃圾清理：E1。Job catalog 可在重启后重建，
  单次遗漏由下一周期补偿；要求 overlap/timeout/last result 可见。
- 数据库备份：E3。只有备份文件原子完成并通过最小完整性检查才算成功，不能以 job 启动为完成。
- 用户显式触发的工作流：按步骤副作用最高等级决定；不能统一按 Scheduler 的 E1 处理。

### Agent tasks

- 流式 token、工具进度和临时展示：E0。
- 已登记的周期 Agent task：E1，重启时通过任务定义重建；单次执行要有 execution 记录。
- Agent 创建/修改订阅、删除数据等工具：业务事务按 E2/E3；聊天输出不能替代业务完成证据。
- 会话 stop/cancel：E0 控制信号；被取消工具的底层阻塞 I/O 可能继续，资源所有者必须最终回收。

### Transfer pending / 文件整理

- transfer pending、队列任务和实际文件移动：E3。完成点是文件步骤、历史状态和必要清理均达到一致终态。
- preview、进度和队列长度：E0；重新扫描可生成的候选：E1；完成/失败通知投递：E2。
- 崩溃恢复必须基于稳定源/目标身份和步骤状态，不允许仅凭“历史记录存在”猜测文件操作已完成。

## 重试、幂等与关停

- E0：不重试或仅当前调用内有限重试；队列关停可丢弃，必须记录。
- E1：固定上限或指数退避，下一周期可重建；同一 job key 不并发重叠。
- E2：稳定 idempotency key；原子 claim；指数退避有上限；超过上限进入 dead letter，不无限刷日志。
- E3：步骤级幂等、lease/heartbeat、重启恢复和人工决策入口；外部不可逆步骤必须记录补偿边界。

关停顺序为停止接收新任务、停止 claim、等待有界 drain、释放资源。超过预算的 E2/E3 任务保持持久
pending 状态交由下次启动，不以取消异常写成成功。

## 备选方案与否决原因

- 全部迁 Kafka/Celery：部署和插件兼容成本远高于当前单体所需，否决。
- 全部留进程内并依赖日志补偿：无法关闭 E2/E3 崩溃窗口，否决。
- 一个通用重试装饰器覆盖全部机制：无法表达事务提交点、文件步骤和幂等键差异，否决。

## 验证与演进

- Event Registry 的 `delivery` 字段与本 ADR 同步进入 runtime baseline。
- ARCH-251 已覆盖 Registry 中六种 `durable_required` 事件，并通过 commit 后崩溃、重复 claim、并发
  claim、JSON 快照恢复和 dead-letter 测试；后续新增 E2 事件必须同时提供业务事务边界和恢复测试。
- ARCH-252 将 Scheduler 的定义、触发和执行状态拆分，但不提升不需要 durable 的 E0 信号。
