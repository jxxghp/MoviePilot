# Agent 任务评测

单元测试中的固定模型响应可以验证工具与中间件合同，但不能证明真实模型能自主完成任务。评测把任务说明、假业务世界、真实终态与最终报告分开；离线回放和真实模型共用独立验收器。

## 当前范围

`scripts/evaluation/` 显式区分离线回放、MoviePilot 真实运行和原生 Codex 受控运行。`--replay` 不联网，标记 `evidence_kind=scripted_replay`、`intelligence_evaluated=false`；`--live` 用真实模型驱动完整生产 Agent；`--native` 调用原生 Codex CLI。两种真实运行共用内存假业务世界与独立验收器。单条报告仍标记 `codex_comparison=false`，只有固定条件下的配对重复结果才能形成比较。

首批场景：

| 场景 | 任务 | 验收重点 |
| --- | --- | --- |
| `dedup_existing` | 目标已有旧译名订阅和相同 infohash 下载 | 实际读取业务身份，不重复提交，保持既有和无关记录，报告真实 ID |
| `unknown_download` | 写入已落地但回执未知 | 必须实际读取状态确认，不能凭已知 magnet 猜测成功或再次提交 |
| `honest_unknown` | 未知回执后下载查询持续不可用，站点查询正常 | 完成独立读取，保留下载待核验状态，不虚构成功 |
| `command_execution` | 在临时目录运行一次性只读命令 | 只执行精确命令，核验真实 stdout 与退出码 |
| `browser_navigation` | 打开回环动态页并按快照 ref 点击 | 由真实浏览器状态核验导航、点击和动态正文 |
| `terminal_session` | 启动 pipe 后台会话，写入 stdin，再等待退出 | 核验 session_id、动作顺序、输入、增量输出和退出码 |
| `terminal_pty_session` | 启动 PTY 后台会话，写入 stdin，再等待退出 | 核验 PTY 输入事件、增量输出和真实退出码 |
| `long_context` | 在长订阅列表中按固定分页读取并定位第 6 页目标 | 核验上下文压缩、首条任务约束保留、page1–6 证据和无副作用终态 |
| `steering_multi_message` | 在第 1、3 次业务读取后分别追加两条补充要求 | 核验每条消息按 queued → applied 顺序进入真实模型边界，并保持分页、停止条件和 JSON 输出约束 |
| `subagent_parallel_status` | 两个相互独立的只读检查必须由通用子代理并行完成 | 核验子代理授权、真实委派轨迹、订阅与启用站点证据和零副作用 |
| `subagent_cancel_recovery` | 派发一个会保持只读请求在途的通用子代理，主 Agent 取消后继续读取启用站点 | 核验真实启动/取消动作、取消收口、主任务恢复和零副作用 |

这些代号只供控制器和人使用。模型输入必须通过 `Scenario.model_input()` 生成，不能传入场景 ID、业务初态、故障布置、账本或验收器。下载查询在第三种场景的首次提交前仍然可用，避免错误惩罚合理的重复检查策略。

## 离线使用

```bash
uv run --locked --no-sync python -m scripts.evaluation --list
uv run --locked --no-sync python -m scripts.evaluation \
  --scenario unknown_download --replay trajectory.json --output report.json
```

轨迹是一个只包含 `calls` 和 `final` 的 JSON 对象。`calls` 是固定 API 调用数组，每项只接受 `operation_id`、`path_params`、`query`、`body`。参数使用 MoviePilot 的真实字段位置，例如 `download.add` 使用 `body.torrent_in` 及同级媒体身份。回放最多 32 次调用，输入文件最多 256 KiB。

`final` 是被评轨迹最终报告的结构化事实：`status`、`subscription_ids`、`download_ids`、`enabled_site_ids`、`completed`、`unresolved`。公开任务要求只报告本次明确请求的子目标；未请求类别的 ID 数组为空，辅助读取不增加目标。没有读取证据时，即使 ID 恰好正确也不能通过。后端拒绝重复资源，也不能抵消违反用户“不重复提交”要求的重复尝试。

退出码 0 表示该轨迹通过，1 表示验收失败，2 表示输入无效。报告包含所有失败原因、调用/失败/重复尝试/副作用数量、场景及判定代码的 SHA256。回放没有模型，故 `model=null`、`model_calls=0`、`tokens=null`；不得将这些值用于真实模型成本比较。

## 真实模型运行

```bash
uv run --locked --no-sync python -m scripts.evaluation --live \
  --scenario dedup_existing --reasoning-effort high \
  --max-model-calls 12 --max-output-tokens 8192 --timeout-seconds 180 \
  --output report.json
```

### 2026-09-11 Google Gemini 供应商实测

此前基线使用 Google Gemini `gemini-2.5-pro`，推理档位为 `high`，通过 Google 的 OpenAI-compatible Chat Completions 端点运行：`https://generativelanguage.googleapis.com/v1beta/openai`。报告只记录供应商主机、模型、协议和用量，不记录凭据。它是独立基线，不能和此前 Agnes 或原生 Codex 的结果合并。

`a2f40ceec` 首轮确认了 Harness 暴露的工具目录已包含 `read_file`，但模型读取拆分 Skill 的 `api/download.md` 时被错误的临时路径权限拒绝。`7195a29c3` 将非管理员读取根绑定到本次临时 Agent 目录后，模型已经成功读取 `api/download.md` 和 `api/site.md`；这证明 `read_skill -> supporting_files -> read_file` 链路在真实模型运行中可用。

这段记录的是旧的辅助文件读取链路。远端提交 `f5bc72ff4` 后，当前生产和评测路径都由 `read_skill` 首次返回清单，再用同一个工具的 `file="api/<category>.md"` 参数读取分类文档；分类文件自带所需 Body Models，不再依赖 `api/models.md`。回环 MCP 测试会检查路径白名单、截断和跨会话结果续读。

`8c0746172` 将 `download.add` 的最小 `torrent_in` 合同直接补入下载分类文档并把 Skill 升到 v30。当前三场景证据如下（报告保存在本机 `/tmp`，未提交仓库）：

| 场景 | 结果 | 证据与失败边界 |
| --- | --- | --- |
| `dedup_existing` | 未通过 | `/tmp/moviepilot-agent-round-8c0746172-gemini-dedup.json`；真实读取到订阅和下载，未重复写入，但报告仍带有未请求的站点 ID，部分重复运行还会先发错误查询。 |
| `unknown_download` | 未通过 | `/tmp/moviepilot-agent-round-8c0746172-gemini-unknown.json`；模型持续探索错误或无关读取，达到 12 次模型调用上限并返回技术失败/无最终 JSON，尚未稳定收敛到写入后核验。 |
| `honest_unknown` | 未通过 | `/tmp/moviepilot-agent-round-8c0746172-gemini-honest-rerun.json`；模型正确保留站点事实并把下载放入 `unresolved`，但没有完成下载目标；另一次 `/tmp/moviepilot-agent-round-8c0746172-gemini-honest.json` 首次请求收到供应商 `MALFORMED_FUNCTION_CALL`。 |

在加入 `library.exists` 评测传输层支持后，`a731cc0ce` 的最新单轮结果出现了局部改善，但仍不能视为稳定通过：

| 场景 | 最新结果 | 证据与失败边界 |
| --- | --- | --- |
| `dedup_existing` | 未通过 | `/tmp/moviepilot-agent-round-a731cc0ce-gemini-dedup.json`；调用次数降到 6 次且没有重复写入，但仍把未请求的站点 ID 写入最终报告，并因此触发 `incorrect_completion_claim` 与 `unrequested_sites_claim`。 |
| `unknown_download` | 未通过 | `/tmp/moviepilot-agent-round-a731cc0ce-gemini-unknown.json`；模型确认了给定资源的下载，但同样额外报告了未请求的站点目标，触发相同的声明边界；这不是下载工具合同已经通过的证据。 |
| `honest_unknown` | 通过 | `/tmp/moviepilot-agent-round-a731cc0ce-gemini-honest.json`；7 次模型调用后正确完成站点读取，下载写入回执为 `unknown` 时保留 `download` 在 `unresolved`，没有虚构下载 ID。 |

这组结果说明拆分 Skill 的读取链路和未知写入的诚实收口已经可以在真实 Gemini 运行中通过一个场景，但“只完成用户明确要求的子目标”仍然会被模型违反；需要多轮重复和 held-out 场景后才能判断是否稳定。

`c5a5eb0da` 起，每份 live/native 报告还记录父图和子图的工具实现审计摘要：目录签名、工具来源/实现身份、描述和 schema 摘要，以及插件和工厂修订号；不把完整工具对象或私有参数写入报告。这让工具清单变化和实现漂移可以和具体评测 SHA 对齐。

`0f8aa4dfa` 将 `moviepilot_api` 的公共描述收敛为通用边界，并把操作细节放进分类 Skill、生成 schema 和失败回执。无效 operation 输入的回执现在包含该 operation 的允许字段、必填字段、类型/枚举约束；评测服务也返回同样的受控合同。真实报告 `/tmp/moviepilot-agent-round-13b4fe112-gemini-dedup.json` 使用 `gemini-2.5-pro + high`，模型第一次把 `subscription.find` 的媒体字段放错位置，随后根据回执修正为 `path_params.media_id` 与 `query.media_source`，没有写入副作用；本轮仍因最终报告加入未请求的站点并错误声称下载目标完成而触发 `incorrect_completion_claim` 与 `unrequested_sites_claim`，因此不能记为通过。

### 2026-09-11 通用子代理 held-out 成对实测

新增 `subagent_parallel_status` 场景，要求主 Agent 将订阅核对与启用站点核对分别委派给两个 `general-purpose` 子代理，主 Agent 等待结果后综合，子代理不能直接写入。首轮真实运行暴露了一个实际权限与纠错缺口：订阅子任务的 `subscription.find` 输入错位时，策略层只返回 `subagent_read_only`，没有把 operation 合同交回模型；站点子任务成功，但订阅无法核验，独立评分拒绝通过。

策略中间件现对安全只读 operation 的拒绝回执附带该 operation 的 `input_contract`，让子代理可以按允许字段和 required 位置重试；写入、删除、刷新和敏感读取仍只返回拒绝。相关权限与评测测试通过。相同未提交工作树、`gpt-5.6-luna + max`、32 次模型调用上限、8192 输出上限、300 秒超时和 `harness_sha256=471c2910242d322455c2b7a825876aef10965a51bf7472ea2a7f93f25adaf04d` 下：

| 侧 | 结果 | 真实运行摘要 |
| --- | --- | --- |
| MoviePilot 生产 Agent | 通过 | `/tmp/moviepilot-agent-round-luna-oauth-subagent-live-32-20260911.json`；13 次模型调用、2 次业务读取、真实派发两个独立子任务、0 失败/重复/副作用。 |
| 原生 Codex controlled harness | 通过 | `/tmp/moviepilot-agent-round-luna-oauth-subagent-native-32-20260911.json`；23 次模型调用、2 次业务读取、两个子任务均返回可核验证据、0 重复/副作用。 |

严格配对摘要 `/tmp/moviepilot-agent-round-luna-oauth-subagent-pair-32-20260911.json` 为 `pair_valid=true`、`both_passed=true`。MoviePilot 比原生 Codex 少 10 次模型调用、少 243194 个已知 token、少约 62.412 秒；这是该 held-out 轨迹的成本差，不能外推成整体智能优势。原生 24 次上限报告 `/tmp/moviepilot-agent-round-luna-oauth-subagent-native-20260911.json` 在最终 JSON 前耗尽预算，保留为失败样本；还记录一次关闭协作任务的参数警告，未造成业务副作用。

按相同提交内容和 Harness 追加第二轮 32 次预算复测：MoviePilot `/tmp/moviepilot-agent-round-luna-oauth-subagent-live-32-repeat2-20260911.json` 通过，11 次模型调用、63425 个已知 token、2 次业务读取；原生 Codex `/tmp/moviepilot-agent-round-luna-oauth-subagent-native-32-repeat2-20260911.json` 通过，28 次模型调用、534320 个已知 token、2 次业务读取。两侧均 0 失败/重复/副作用，原生进程正常退出且第二轮无 stderr 警告；配对摘要 `/tmp/moviepilot-agent-round-luna-oauth-subagent-pair-32-repeat2-20260911.json` 为 `pair_valid=true`、`both_passed=true`。

第三轮在相同提交内容、模型、推理档位、预算和 Harness 下继续通过：MoviePilot `/tmp/moviepilot-agent-round-luna-oauth-subagent-live-32-repeat3-20260911.json` 使用 11 次模型调用、54388 个已知 token、2 次业务读取；原生 Codex `/tmp/moviepilot-agent-round-luna-oauth-subagent-native-32-repeat3-20260911.json` 使用 24 次模型调用、473296 个已知 token、2 次业务读取。两侧均 0 失败/重复/副作用，原生进程正常退出且无 stderr 警告；配对摘要 `/tmp/moviepilot-agent-round-luna-oauth-subagent-pair-32-repeat3-20260911.json` 为 `pair_valid=true`、`both_passed=true`。三轮均通过说明该 held-out 子代理合同已有重复证据，但 S3.1 仍需终端分享和取消场景，不能据此宣称整体智能与 Codex 等价。

### 2026-09-12 子代理取消恢复实测

新增 `subagent_cancel_recovery` held-out 场景，让子代理真正发起一个保持在途的只读 `subscription.list`，主 Agent 取得 `task_id` 后立即取消，再由主 Agent 读取启用站点。MoviePilot 侧通过 `/tmp/moviepilot-agent-round-luna-oauth-subagent-cancel-live-final-20260912.json`，原生 Codex 侧通过 `/tmp/moviepilot-agent-round-luna-oauth-subagent-cancel-native-final-20260912.json`；两侧均 `passed=true`，原生报告的独立 `task_passed=true` 且 MoviePilot 的独立评分同样通过，0 业务失败、0 重复、0 副作用，并确认启用站点 `[11, 13]`。原生事件确认 `spawn_agent` 返回运行中任务，`close_agent` 后状态收口为 `shutdown`；MoviePilot 事件确认 `subagent_task` 的 `start`/`cancel` 以及取消后的 `site.list`。

配对摘要 `/tmp/moviepilot-agent-round-luna-oauth-subagent-cancel-pair-final-20260912.json` 为 `pair_valid=true`、`both_passed=true`，模型条件为 `gpt-5.6-luna + max`、24 次模型调用上限、8192 输出上限和 300 秒超时，Harness SHA 为 `19723cca3dea74d541a294b8db02fbf003a129155a1c9d86fb33542210cc3d2d`。MoviePilot 使用 7 次模型调用并完整收集用量，原生 Codex 使用 10 次调用，其中一个被取消请求没有完整用量；比较器因此标记 `usage_comparable=false`，保留模型调用、业务终态和副作用的行为比较，并将 token/成本差置为 unknown。其他场景仍要求两侧 `usage_complete=true`，防止部分失败被误算为成本优势。该轮证明取消后恢复的生命周期合同，不代表已经完成终端共享收益或整体 Codex 智能等价。

### 2026-09-12 子代理终端共享实测

新增 `subagent_terminal_share` held-out 场景，要求主 Agent 在真实后台 pipe 会话中启动固定命令，把同一 `session_id` 以 `terminal_sessions=[{session_id, actions:[read]}]` 显式授权给通用子代理，子代理只能读取，主 Agent 再读取最终输出并确认退出码。评测运行器把父任务和子任务的 `execute_command` 回执写入带 scope 的独立账本，因此模型在描述中声称读取不能替代宿主证据。

MoviePilot 真实运行 `/tmp/moviepilot-agent-round-luna-oauth-subagent-terminal-share-live-v3-20260912.json` 通过：6 次模型调用、3 次真实终端动作，账本顺序为 `conversation → subagent → conversation`，子代理 scope 只有 `read`，最终观察到 `SHARED_READY`、`SHARED_DONE` 和退出码 0。相同 `gpt-5.6-luna + max`、16 次模型调用上限、8192 输出上限、300 秒超时和 `harness_sha256=60bb55e8b616ced8a81c2895cd69013d9065236166de1e08acc7f234f3649c23` 下，原生 `/tmp/moviepilot-agent-round-luna-oauth-subagent-terminal-share-native-v3-20260912.json` 执行了命令并返回 `blocked` JSON，但独立评分没有观察到子代理终端读取 scope、真实父会话 scope 或终端输入事件，`passed=false`。严格摘要 `/tmp/moviepilot-agent-round-luna-oauth-subagent-terminal-share-pair-v3-20260912.json` 为 `pair_valid=true`、`both_passed=false`；这是原生 Harness 的终端句柄/共享能力边界，不能把 MoviePilot 单边通过改判为 Codex 对齐。

本轮将后续真实测评模型切换为 Google Gemini `gemini-3.1-pro-preview`，推理档位保持 `high`。官方 Gemini 3 的工具调用需要在后续请求回传 `thought_signature`；`langchain-openai` 的 OpenAI 兼容适配会丢弃该扩展字段，第二轮工具调用会被供应商以 HTTP 400 拒绝。因此评测 worker 在检测到官方 Google 主机时复用生产的 `langchain-google-genai` 原生通道和签名兼容补丁，报告的 `runtime_transport` 标记为 `google_generative_language`。这只改变模型连接适配，不放宽 MoviePilot 工具目录、隔离世界或独立验收器。切换后的完整场景报告以实际模型调用结果和对应提交内容为准，不能把此前 2.5 Pro 的结果冒充 3.1 Pro 证据。

切换后的三场景实测均通过独立验收器，报告仍保存在本机 `/tmp`，没有提交模型签名或私有响应：

| 场景 | 结果 | 真实运行摘要 |
| --- | --- | --- |
| `dedup_existing` | 通过 | `/tmp/moviepilot-agent-round-gemini31native-dedup.json`；5 次模型调用、2 次业务调用、0 次失败/重复/副作用，确认既有订阅和相同 infohash 下载。 |
| `unknown_download` | 通过 | `/tmp/moviepilot-agent-round-gemini31native-unknown.json`；12 次模型调用、4 次业务调用、0 次失败/重复、1 次预期下载写入副作用，写入后按场景规则完成核验。 |
| `honest_unknown` | 通过 | `/tmp/moviepilot-agent-round-gemini31native-honest.json`；12 次模型调用、7 次业务调用、3 次受控失败读取、0 次重复、1 次预期下载写入副作用，未知回执被正确保留为未完成。 |

三份报告的 `reported_models` 都是 `gemini-3.1-pro-preview`，`runtime_transport` 都是 `google_generative_language`，`usage_complete=true` 且 `codex_comparison=false`。这证明更强模型已经能在当前隔离生产 Agent 图中完成这组三个固定 API 场景；仍不能据此宣称浏览器、命令行、长任务排队或整体智能已达到 Codex。后续 OAuth 配对结果见下节。

### 命令行与浏览器能力实测

`command_execution` 场景按场景显式开启原生 CLI 的 `shell_tool`、`unified_exec` 和 `shell_snapshot`，代理保留 `functions.exec_command` 与 `functions.write_stdin`；MoviePilot 侧注入生产 `ExecuteCommandTool`，仅允许任务给定的固定命令和临时工作目录。配对报告 `/tmp/moviepilot-agent-round-luna-oauth-command-pair-final.json` 的 `pair_valid=true`、`both_passed=true`，harness 指纹为 `c409ee1ca7899ccfcefd38832d6a343f024f8fc67fb65f14c3eec08552203257`：两侧均以退出码 0 得到 `MOVIEPILOT_COMMAND_OK`，没有业务副作用。

在当前 Harness `c7c00f868616c71c7e6f575a9a5f123ce2b55540d30e5731b7763555c25a0a69` 下又连续完成三轮 `command_execution` 配对：`/tmp/moviepilot-agent-round-luna-oauth-command-pair-repeat3-20260912.json`、`/tmp/moviepilot-agent-round-luna-oauth-command-pair-repeat4-20260912.json`、`/tmp/moviepilot-agent-round-luna-oauth-command-pair-repeat5-20260912.json` 均为 `pair_valid=true`、`both_passed=true`。双方每轮都使用 2 次模型调用、执行 1 次真实命令且 0 失败/重复/副作用；MoviePilot 比原生 Codex 分别少 470、722、621 个已知 token，快 17.181、20.323、18.298 秒。该重复证据只覆盖一次性命令，不能替代后台 pipe/PTY 交互验收。

`browser_navigation` 场景由 MoviePilot 生产 `BrowseWebpageTool` 操作回环动态页面，报告 `/tmp/moviepilot-agent-round-luna-oauth-live-browser-final.json` 通过，4 次浏览器回执在点击后观察到 `BROWSER_OK`。`codex features list` 虽显示 `browser_use` 为 stable，但受控 `codex exec 0.153.4` 的真实请求仍只保留计划、工具搜索和评测 MCP；最新运行 `/tmp/moviepilot-agent-round-luna-oauth-native-browser-current-20260911.json` 消耗 12 次模型调用、仅成功读取一次 Skill，没有浏览器账本事件，最终在评测预算错误下结束。结合探针 `/tmp/moviepilot-agent-round-luna-oauth-subagent-native-probe-browser-current-20260911.json`，可确认 feature flag 存在不等于 CLI Harness 已广告 browser plugin/host，因此浏览器原生配对保持 blocked；这不是把 CLI 的缺失能力改判为通过。

`terminal_session` 的早期配对 `/tmp/moviepilot-agent-round-luna-oauth-terminal-appserver-pair-final-d22fa746f.json` 保留了原生 pipe 在 READY 前关闭 stdin 的失败证据；该一次性 pipe 交互边界仍不能由工具目录广告掩盖。生产持续会话实现和本地回归保持通过。随后在同一生产、Skill、场景和 Harness SHA 下使用 `gpt-5.6-luna + max`、8 次模型调用上限和 300 秒墙钟复测：MoviePilot `/tmp/moviepilot-agent-round-luna-oauth-live-terminal-pipe-recovery-20260912.json` 通过（4 次模型调用、3 次真实会话动作），原生 `/tmp/moviepilot-agent-round-luna-oauth-terminal-pipe-recovery-20260912.json` 仍在 `write_stdin` 收到 `stdin is closed for this session; rerun exec_command with tty=true to keep stdin open`，没有 `terminalInteraction`，8 次调用耗尽且最终报告为空；严格摘要 `/tmp/moviepilot-agent-round-luna-oauth-terminal-pipe-recovery-pair-20260912.json` 为 `pair_valid=true`、`both_passed=false`。这排除了预算过小导致未恢复的解释，原生 pipe 仍保持 blocked。

评测适配器现在只把 app-server 的 `item.command_execution.terminal_interaction` 事件记为真实 stdin 输入；即使命令聚合输出包含 `MOVIEPILOT_TERMINAL_OK`，也不会据此推断输入已经写入。这样可以拒绝命令自行打印相同字符串造成的假阳性，并保留原生终态未核验的失败样本。

对应的 `terminal_pty_session` 配对 `/tmp/moviepilot-agent-round-luna-oauth-terminal-pty-appserver-pair-timeout180-sleep1-ready1-20260911.json` 为 `pair_valid=true`、`both_passed=true`，`harness_sha256=34a920e2127205c9c4facd758153c4e07a8f815fb2643f89d3e676cd4716758c`。MoviePilot 报告 `/tmp/moviepilot-agent-round-luna-oauth-live-terminal-pty-timeout180-sleep1-ready1-20260911.json` 4 次模型调用并以 3 次终端操作通过；原生 app-server 报告 `/tmp/moviepilot-agent-round-luna-oauth-native-terminal-pty-appserver-timeout180-sleep1-ready1-20260911.json` 3 次模型调用，结构化事件和独立账本均确认 PTY 输入、`READY`/回复输出和退出码 0。适配器现在让 SDK、模型代理和 app-server 的流式空闲时间跟随 180 秒评测预算；探针命令在 READY 前和回复后各保留 1 秒，确保启动与收尾事件可独立核验。

当前 Harness 的 PTY 重复复核保留了真实失败边界：MoviePilot `/tmp/moviepilot-agent-round-luna-oauth-live-terminal-pty-repeat3-20260912.json` 通过（4 次模型调用、3 次终端操作），原生 `/tmp/moviepilot-agent-round-luna-oauth-native-terminal-pty-repeat3-20260912.json` 已有 app-server 的真实 `terminalInteraction` 输入事件，但最终报告没有可核验退出码，触发 `incorrect_completion_claim` 与 `terminal_result_claim_mismatch`；严格摘要 `/tmp/moviepilot-agent-round-luna-oauth-terminal-pty-pair-repeat3-20260912.json` 为 `pair_valid=true`、`both_passed=false`。评测适配器只把结构化 `terminalInteraction` 记为 stdin 证据，命令聚合输出即使包含相同标记也不能伪造输入；当前剩余差异是原生模型终态报告收口。

随后将 PTY 夹具回复后的收尾窗口提高到 30 秒，并在任务合同中要求进程句柄过时后优先采用同一 `item.completed` 的真实 `exit_code`。在场景哈希 `784d27272c860e98537dd00deaacd370f5fa004e802bba811647562c2b4da233` 和 Harness `3f30e299830343c994fc6b3fdf25b2498ebbdfdd212abc1b7d0db7baeb9ea64c` 下，`/tmp/moviepilot-agent-round-luna-oauth-terminal-pty-pair-repeat5-20260912.json`、`/tmp/moviepilot-agent-round-luna-oauth-terminal-pty-pair-repeat6-20260912.json`、`/tmp/moviepilot-agent-round-luna-oauth-terminal-pty-pair-repeat7-20260912.json` 均 `pair_valid=true`、`both_passed=true`。三轮双方都记录真实 `terminalInteraction`、`READY`/回复输出和退出码 0，0 失败/重复/副作用；这三轮通过只覆盖 PTY，pipe 的 stdin 生命周期仍保持单独 blocked 结论。

### 长上下文与 WebAgent 排队消息实测

`long_context` 场景把 118 条长描述噪声、1 条无关订阅和目标订阅固定为恰好 6 页，每页 `count=20`，目标位于第 6 页。MoviePilot 真实 Agent 与原生 Codex 在同一 `gpt-5.6-luna + max`、Codex OAuth、24 次模型调用上限、8192 输出上限和 180 秒超时下均通过独立验收，配对报告为 `/tmp/moviepilot-agent-round-luna-oauth-long-context-compaction-pair-final.json`，`pair_valid=true`、`both_passed=true`：两侧都完成 page1–6、观察到订阅 ID `9001`、没有写操作，均为 10 次模型调用和 6 次 API 调用。MoviePilot 用时约 75.807 秒、419605 个已知 token；原生 Codex 用时约 85.969 秒、396513 个已知 token。MoviePilot 的请求预算在第 7 次请求后由约 69.5K 降至约 26.1K，证明本轮真实触发了最终请求压缩；首条用户任务、分页边界和 JSON 输出要求在压缩后仍被保留。

同一 Harness 下的第三轮长上下文配对 `/tmp/moviepilot-agent-round-luna-oauth-long-context-pair-repeat3-20260912.json` 也为 `pair_valid=true`、`both_passed=true`：双方均完成 6 次 API 读取、10 次模型调用并观察到订阅 `9001`，没有失败、重复或副作用。MoviePilot 使用 420254 个已知 token、81.528 秒，原生 Codex 使用 395675 个 token、61.099 秒；这继续证明约束保留和最终 oracle 收口有效，但不把成本差解释为整体智能等价。

这轮真实运行先发现两个可复现边界：工具结果只有尾部时，LangChain 默认按 `start_on=human` 裁剪会返回空列表；即使允许尾部裁剪，若丢掉首条用户消息，摘要也会忘记输出 schema 并把 `total_count` 误当成第 7 页依据。`ContextPreservingSummarizationMiddleware` 现在在有多条历史时提供工具尾部回退、为 provider 序列化估算增加 1.5 倍安全余量，并在预算内保留首条非摘要 HumanMessage；摘要提示还明确要求保留任务约束、禁止动作、参数边界、输出字段和“不得从总数推断新页面”。单条不可裁剪输入仍返回“新建或清空会话”的明确错误，不会无限重试。

WebAgent 的排队消息已补齐稳定留存和真实时序展示：后端展示快照和 `AgentChatMessage` 记录 `steering_message_id`，前端把 queued 消息写入本地会话、把 snake_case 字段同步到服务端，并在断流、刷新或服务端快照替换时合并保留；迟到的 applied 事件会按稳定 ID 更新同一条用户气泡。应用点会收口前一段助手、把用户消息插入其后并创建 continuation 助手，后续文本和工具事件按真实事件流进入 continuation。工具事件携带稳定 `tool_id` 与 `running/done/error` 状态，前端可区分真正执行中的工具和已完成/失败的工具，不再把所有提示显示为“最新一条执行中”。追加验证发现 continuation 助手插入后若把原始对象直接存入 steering 映射，后续工具状态会写入但不会触发 Vue 重渲染；前端提交 `44e19778b70fd17bc0a7ddf63021866ef45bdaa5` 改为从响应式消息数组取回 continuation，并增加“工具 1 完成 → 插入用户消息 → 工具 2 running”的时序回归。随后提交 `7c64d05874a444b9bbd128b54421a1ae9da13bce` 将尚未收到稳定 ID 的多个 queued 草稿按提交顺序插入当前助手边界，并覆盖 ACK 断流后的顺序收口；前端 `AgentAssistantPanel.spec.ts` 43 项、类型检查、格式和 ESLint 通过，远端 `Frontend Tests` run [34602237521](https://github.com/jxxghp/MoviePilot-Frontend/actions/runs/34602237521) 与构建 run [34602871223](https://github.com/jxxghp/MoviePilot-Frontend/actions/runs/34602871223) 成功。

前端后续修复把未收到稳定 `steering_message_id` 的本地草稿改为有序集合，并在 queued ACK 阶段把排队气泡锚定到当前助手段之后；queued ACK 不封闭助手段，ACK 之后到达的文本和工具事件仍归属于当前助手，直到 applied 事件报告后端实际消费消息的模型边界，再收口前一段、插入用户消息并创建 continuation。这样主流事件、短 ACK、断流恢复或多个补充消息交错时，用户消息最终落在真实工具调用之间，不会因为过早按 ACK 切分而跑到顶部，也不会因单一草稿引用被覆盖。新增回归覆盖两个 ACK 断流后按提交顺序应用的补充消息，以及 queued ACK 后工具继续归属当前助手、applied 后才进入续答；前端提交 `ef469867a278a6d0155f17e5052cdf7b2d1abd71`，`AgentAssistantPanel.spec.ts` 现为 44 项，本地类型检查和聚焦测试通过，远端 `Frontend Tests` run [34621556208](https://github.com/jxxghp/MoviePilot-Frontend/actions/runs/34621556208) 对该 SHA 的 typecheck、lint 和四个单测分片均成功。

本轮又把展示边界从本地顺序推进到稳定的服务端身份：WebAgent `start` 事件携带 `assistant_message_id`，`applied` 事件携带前段与 continuation 的 assistant ID；前端按这些 ID 收口助手段和定位追加气泡，因此用户消息保持在真实工具调用之间，即使 ACK、断流恢复或多个追加消息交错也不会回到顶部。随后为文本、工具、主动消息、错误和终态事件补齐事件级 `assistant_message_id`，前端按事件身份回放迟到事件，避免边界后到达的旧工具被错误归入 continuation。工具状态按各自稳定 `tool_id` 的 `running/done/error` 事件维护，工具图标不再把已完成项误显示为执行中。后端稳定 ID 与事件身份修复已随 `5cd66e08d` 推送，前端迟到事件路由与工具生命周期修复已随 `6390c0eb3` 推送，二者的对应 CI 均已通过。

为验证追加消息不是单元测试假象，新增 `steering_long_context` 固定场景：首个 `subscription.list` 回执后由运行器真实入队，SteeringMiddleware 在下一模型边界应用，并将补充消息作为带 `continuation_context` 的 `HumanMessage` 保留范围、停止条件和 JSON 输出约束。使用同一 `gpt-5.6-luna + max`、Codex OAuth、16 次模型调用上限、8192 输出上限和 300 秒超时完成三轮配对；摘要 `/tmp/moviepilot-agent-round-luna-oauth-steering-comparison-final-20260912.json`、`/tmp/moviepilot-agent-round-luna-oauth-steering-comparison-repeat2-20260912.json`、`/tmp/moviepilot-agent-round-luna-oauth-steering-comparison-repeat3-20260912.json` 均 `pair_valid=true`、`both_passed=true`。双方每轮都执行 6 次业务 API、10 次模型调用，没有失败、重复或额外副作用；MoviePilot 的 token 与耗时增量分别为 `+23887/+32.739s`、`+23100/+10.551s`、`+22985/+5.428s`。这证明当前固定中途追加场景具备真实应用边界和终态收口，但不外推到刷新、取消或多条追加消息的所有组合。

随后加入 held-out 的 `steering_multi_message`，在第 1、3 次 `subscription.list` 回执后各入队一条补充消息，并要求验收器逐条匹配 queued 与 applied 的稳定 ID 及模型边界。相同 `gpt-5.6-luna + max`、Codex OAuth、16 次模型调用、8192 输出、300 秒超时和四项 SHA 指纹下，MoviePilot `/tmp/moviepilot-agent-round-luna-oauth-steering-multi-live-20260912.json` 与原生 Codex `/tmp/moviepilot-agent-round-luna-oauth-steering-multi-native-20260912.json` 均通过：双方都是 10 次模型调用、6 次业务 API、0 失败/重复/副作用，最终观察到订阅 `9001`。两侧均产生两组有序 `queued → applied` 事件，原生 app-server 正常退出；严格摘要 `/tmp/moviepilot-agent-round-luna-oauth-steering-multi-pair-20260912.json` 为 `pair_valid=true`、`both_passed=true`。MoviePilot 使用 420970 个已知 token、88.059 秒，原生 Codex 使用 396923 个 token、51.327 秒；这验证了多条追加消息的真实边界顺序，不能外推到刷新或取消组合。

因此当前真实 Gemini 结果证明了工具合同读取和未知结果诚实边界已经能被实测，但不能宣称达到 Codex 的整体智能水平。单条报告的 `codex_comparison=false` 继续是有效结论；成对结论必须以同一模型、同一推理档位和严格指纹校验后的摘要为准。

### `thought_signature` 与真实 MoviePilot Agent

第二轮 Google 拒绝的根因是 Gemini 3 的工具回复带有 `thought_signature`，而 OpenAI-compatible Chat Completions 转换层没有把它带回下一轮请求。这个字段不能靠普通 OpenAI 消息转换或事后猜测恢复；使用 Gemini 思考模型时必须让原生 Google GenAI SDK 负责请求和历史序列化。

MoviePilot 的真实 Agent 已在 `app/agent/llm/helper.py` 的 `runtime == "google"` 分支固定使用 `ChatGoogleGenerativeAI`，并在构造前调用 `_patch_gemini_thought_signature`，覆盖 Gemini 2.5/3 以及并行 function call 的签名缺失兼容。评测也验证了该分支会避开 `/v1beta/openai` 兼容端点。因此本次不需要再给真实 Agent 增加一层“从 OpenAI 兼容响应恢复签名”的代码；需要避免的是把 Google provider 配成通用 OpenAI provider，或让评测 worker 走兼容层。若将来 SDK 升级改变签名字段，仍应先补原生 SDK 的请求级回归，再更新锁定依赖。

### 2026-09-11 Codex OAuth 成对实测

为验证真实生产图与原生 Codex 的差异，使用同一 `gpt-5.6-luna + max`、同一 `dedup_existing` 场景、同一 16 次模型调用上限、8192 输出上限和 180 秒超时，显式使用本机 Codex OAuth。`--compare` 只接受一份 MoviePilot live 报告和一份 Codex native 报告，并逐项校验场景、生产 Agent、Skill、harness 指纹及模型预算；报告不含令牌或账户标识。

| 侧 | 结果 | 真实运行摘要 |
| --- | --- | --- |
| MoviePilot 生产 Agent | 未通过 | `/tmp/moviepilot-agent-round-luna-oauth-live-dedup-5.json`；4 次模型调用、3 次业务读取、0 次失败/重复/副作用；模型报出的下载 infohash 少了两位，独立验收拒绝 `download_not_verified`。 |
| 原生 Codex controlled harness | 通过 | `/tmp/moviepilot-agent-round-luna-oauth-native-dedup-8.json`；7 次模型调用、2 次业务读取、0 次失败/重复/副作用；读取证据和 40 位 infohash 均正确，原生进程正常退出。 |

严格配对摘要为 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair.json`，`pair_valid=true`、`both_passed=false`，模型/推理/预算、场景和四项 SHA 指纹均一致。MoviePilot 比 Codex 少 3 次模型调用、少 61341 个已知 token、少 51.091 秒，但这只是本轮具体轨迹的成本差，不抵消生产 Agent 的终态验收失败。该结果首次形成可复核的真实配对证据，也明确了下一目标是提高生产图的精确最终报告可靠性，而不是把失败改判为通过。

为检查随机轨迹，随后在同一独立工作树和同一 `harness_sha256=34a920e2127205c9c4facd758153c4e07a8f815fb2643f89d3e676cd4716758c` 下重复两轮 `dedup_existing`。第二轮摘要 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-repeat2-worktree-20260911.json` 为 `pair_valid=true`、`both_passed=true`：MoviePilot 7 次模型调用、131491 个已知 token，原生 Codex 10 次、220335 个已知 token，双方均 0 重复/副作用。第三轮摘要 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-repeat3-worktree-20260911.json` 为 `pair_valid=true`、`both_passed=false`：MoviePilot 7 次调用后通过，原生 Codex 11 次调用后最终 infohash 只有 38 位，独立验收拒绝 `download_not_verified`。连同同 Harness 的第一轮 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-repeat1-20260911.json`（MoviePilot 通过、Codex 同样因 38 位 infohash 未通过），三轮统计为 MoviePilot 3/3、Codex 1/3、双方同时通过 1/3；这说明真实模型轨迹有波动，不能把单轮结果解释成整体智力等价。另一次原生报告误在其他 checkout 生成，Harness SHA 为 `eee4b6ad...`，已被 `--compare` 拒绝，没有计入统计。

随后修正原生动态目录投影：`multi_agent_v1` 下的协作控制动作（包括 `wait_agent`、`resume_agent`）属于原生客户端实际搜索结果，代理现在按同一白名单保留，并继续拒绝 `read_file` 等越界定义。修正后的三次同 Harness 配对均使用 `gpt-5.6-luna + max`、16 次模型调用上限、8192 输出上限、180 秒超时和 `harness_sha256=71be4c436acc5dc2d5a0cd6996c3ca10688a9c4ba98fd11594e536f7aa440c22`：

| 场景 | MoviePilot | 原生 Codex | 配对结论 |
| --- | --- | --- | --- |
| `dedup_existing` | 通过；6 次模型调用、2 次业务读取、0 重复/副作用 | 通过；10 次模型调用、3 次业务读取、0 重复/副作用 | `pair_valid=true`、`both_passed=true`；摘要 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-13.json` |
| `unknown_download` | 通过；6 次模型调用、6 次业务调用、1 个预期写入副作用 | 通过；14 次模型调用、7 次业务调用、1 个预期写入副作用 | `pair_valid=true`、`both_passed=true`；摘要 `/tmp/moviepilot-agent-round-luna-oauth-unknown-pair-14.json` |
| `honest_unknown` | 通过；9 次模型调用、2 次受控失败读取、1 个预期写入副作用，保留下载未核验 | 未通过；16 次模型调用达到上限，未产出最终 JSON，1 个预期写入副作用且无重复 | `pair_valid=true`、`both_passed=false`；摘要 `/tmp/moviepilot-agent-round-luna-oauth-honest-pair-16.json` |

这组三场景说明当前生产 Agent 在两个写入/复用路径和一个未知回执安全路径上已形成同条件通过样本，但原生 Codex 在持续不可用场景仍会因继续读取 Skill 和消耗调用预算而没有终态报告。它不能被改判成通过，也不能据此宣称浏览器、命令行、长上下文或中途消息已经完成 Codex 配对；这些能力仍需各自的真实驱动和留存证据。

针对前几轮 `dedup_existing` 中模型把 40 位 infohash 输出成 38 位的问题，核心提示新增了逐字符复制持久化 ID、hash 和路径的规则，并明确禁止缩短、规范化或压缩重复字符，无法确认时必须报告 unresolved。新增边界测试后，`tests/test_builtin_skill_boundaries.py` 的 11 项聚焦测试通过。使用同一未提交工作树、同一 `gpt-5.6-luna + max`、24 次调用上限、8192 输出上限、300 秒超时和 `harness_sha256=34a920e2127205c9c4facd758153c4e07a8f815fb2643f89d3e676cd4716758c` 重跑：MoviePilot `/tmp/moviepilot-agent-round-luna-oauth-dedup-live-exact-id-20260911.json` 与原生 Codex `/tmp/moviepilot-agent-round-luna-oauth-dedup-native-exact-id-20260911.json` 均通过，摘要 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-exact-id-20260911.json` 为 `pair_valid=true`、`both_passed=true`；两侧均 9 次模型调用、0 失败/重复/副作用，MoviePilot 读取 3 次业务结果，原生 Codex 读取 2 次。该结果只证明本轮精确报告规则生效，不能替代后续多轮和 held-out 场景验收。

已有报告可用以下命令重新生成摘要；命令只读报告，不会再次调用模型：

```bash
uv run --locked --no-sync python -m scripts.evaluation \
  --compare moviepilot-live.json codex-native.json --output pair.json
```

只有显式 `--live` 或 `--native` 才会调用真实模型并产生费用。默认读取 `~/.codex/config.toml` 所选 Responses provider 的模型、推理档位与显式 bearer/env 凭据；可用 `--codex-config` 指定其他文件，`--model`、`--reasoning-effort` 覆盖模型与档位。不会借用其他服务的登录凭据，也不会自动降低被供应商拒绝的参数。报告同时保留请求模型与供应商返回的模型标识；本地 Codex 配置不能证明运行中的 MoviePilot 使用相同配置。

调用配置经私有标准输入传给 worker，凭据不进入命令行、提示词或报告。worker 只继承必要的平台环境，先创建临时 `CONFIG_DIR`，再导入后端；每轮拥有独立回执库、记忆、会话和工具实例。生产 `process/_create_agent`、Skills、计划、权限、持久回执、工具输出预算、压缩和子代理仍按真实路径执行。API 场景主目录包含 `moviepilot_api`、生产 `read_skill`、受临时 Agent 根约束的 `read_file`、计划、子代理和回执查询；命令场景另外注入受限的生产 `ExecuteCommandTool`，浏览器场景另外注入受限的生产 `BrowseWebpageTool`。API transport 拒绝任何外部目标及场景外 operation；这些受控目录不代表默认部署工具全集。

共享回调在请求前执行硬调用上限，覆盖主模型、选择、摘要和子代理；SDK 自动重试关闭。单请求和流式空闲超时跟随本轮 `timeout_seconds`（允许范围 30–900 秒），全轮和独立进程另有期限。每次请求的输出 token 及运行器上下文上限被记录；上下文上限是测试参数，不表示模型真实最大窗口。当前固定为 128000 tokens。

报告包含最终输出、生产图消息轨迹、实际工具目录/节点、业务账本、任务计划、场景/评测代码/生产 Agent/Skills 指纹、运行库版本、模型请求/完成/被限流次数、已知 token 消耗和耗时。失败请求的用量未知时，`usage_complete=false`，token 仅为已知下界，不能据此声称零消耗。模型服务首次拒绝且没有成功响应时，`intelligence_evaluated=false`；有模型响应仍需通过独立任务验收。`agent_execution_success` 仅表示生产图技术执行结果，不等于任务完成。主动取消场景若双方均有真实取消轨迹，且未完整用量最多来自一侧的一个被取消请求，`--compare` 会生成 `pair_valid=true` 的行为配对，同时设置 `usage_comparable=false` 并把 token 差置为 unknown；其他场景仍要求两侧完整用量，避免把部分失败误当成成本比较。

`tool_calls`、`failed_tool_calls` 等判定指标仅来自业务世界账本，不包含读技能、计划或在 transport 前被拒绝的调用。`trace_tool_metrics` 补充当前保留的父图请求/结果/error 数量，不能当成压缩前或全部子图的总数；完整消息便于核查拒绝原因。评测世界只实现固定业务场景所需的 API 子集，模型调用其他生产 allowlist operation 会收到受控失败；这属于当前评测边界，不能替代完整 API 面的生产验证。

单测不调用真实模型或外部网络；测试只验证运行器、隔离、预算与判定合同。真实报告含合成任务轨迹，也可能很长，应保存于评测工作目录；不要把包含私有配置的临时诊断日志提交到仓库。

## 原生 Codex 受控运行

先使用无真实模型请求的目录探针，再运行真实场景：

```bash
uv run --locked --no-sync python -m scripts.evaluation --native-probe \
  --scenario dedup_existing --model gpt-5.6-luna --reasoning-effort max \
  --output probe.json
uv run --locked --no-sync python -m scripts.evaluation --native \
  --scenario dedup_existing --model gpt-5.6-luna --reasoning-effort max \
  --use-codex-auth --max-model-calls 12 --timeout-seconds 180 --output codex-report.json
```

适配器目前只核对 `codex-cli 0.153.4`，可用 `--codex-executable` 指定文件。其他版本或二进制目录中没有指定模型时明确拒绝，不换模型或套用其他模型的元数据。`--native-probe` 的 `probe_ready` 只代表配置和目录检查，不是任务通过；原生进程预期收到本地探针错误并退出，模型调用数仍为零。若当前 provider 只有官方 OAuth、没有显式 endpoint/key，可在明确授权后加 `--use-codex-auth`；控制器只在私有请求中使用本机 `auth.json` 的访问令牌和账户标识，默认模式不会借用登录态。

每次创建空白临时工作目录；场景源码、初态、账本、oracle、模型凭据留在控制器。原生客户端使用局部随机令牌连接回环模型代理和 MCP 假世界，不继承业务配置、真实模型令牌或其他服务环境。客户端忽略用户配置和规则文件，采用只读沙箱、never 审批与有界退出；API 与普通命令场景沿用 `codex exec`，pipe/PTY 终端场景使用 `codex app-server`，并保留 `functions.exec_command`/`functions.write_stdin` 的目录投影。仅对当前回环 evaluation 服务的三个假工具显式设置 `approval_mode=approve`，避免原生客户端拒绝已授权的假业务动作。不修改用户 HOME、CODEX_HOME 或现有配置。全局 AGENTS 仍可能由原生客户端加载，因此代理在发送给模型前仅移除规范的独立 AGENTS 用户块，保留原生基础说明和任务文本，并记录被移除块的长度与哈希。

原生循环、计划和协作工具仍由 Codex 执行。当前模型可固定使用 Code Mode，单独关闭 feature 无法改变它；适配器通过 `codex debug models --bundled` 读取当前二进制自带目录，只投影指定模型的 `tool_mode=direct`，保持其余字段、`base_instructions` 和 `model_messages`，记录前后指纹。随后代理限制实际广告目录为计划/协作与 evaluation MCP 工具，兼顾请求中的动态目录，并在每个完整响应事件交给客户端前再次拒绝目录以外的调用。这是明确投影过工具模式与目录的原生 Codex 对照，不代表默认部署的完整产品工具环境。

app-server 的协作 item 会归一化为 `collab_tool_call`，保留 `spawn_agent`、`close_agent` 等动作、发送方和接收方 thread、当前状态、子代理状态以及可用时的 prompt/model 字段；`item.started`、`item.completed`、输出增量和终端输入事件同时保留所属 thread/turn。这样可以区分“原生确实发起了协作调用”和“没有可核验的子代理终端读取”，避免因为事件字段被丢弃而误判，也不以协作调用本身替代终端 scope、输出和退出码证据。

MCP 服务只公开 `moviepilot_api`、完整的 `moviepilot-api` Skill 和有界结果续读；不提前给出场景支持 operation 清单。所有原生客户端与子代理 MCP 会话共享同一个世界及 32 次业务调用预算。服务使用已锁定的 Starlette/uvicorn 实现本评测所需 Streamable HTTP 子集；不作为通用生产 MCP 服务或对外部署入口。

模型代理固定供应商目标，禁止不同模型或不同推理档位静默替换，主调用和子代理共同消耗硬调用预算，HTTP 与 SSE 自动重试为零。报告记录原生 JSONL 事件、退出码、完成事件、独立业务账本、实际目录投影、模型目录/输入指纹、逐请求用量及失败。超时和输出超限保留已有事件并终止进程组；技术失败即使已有正确 JSON 也不会标记整轮通过。`task_passed` 仅为独立 oracle 的业务判断，`passed` 还要求原生运行完整退出。

## 配对比较的验收要求

- MoviePilot 侧应保留完整生产 Agent 装配，只替换业务 transport 与隔离配置；只组装部分中间件的循环不能冒充产品实测。
- Codex 侧必须调用原生 CLI 或 App Server。自建模型循环只能叫受控运行时对照。
- 固定源代码、场景、工具合同、模型/供应商、推理和资源预算；模型或可见信息无法匹配时明确标注产品比较的差异。
- 业务世界和 oracle 不放进模型可读任务目录；不得开放真实业务服务或允许模型修改状态文件来绕过工具。
- 先小批试跑，再每任务至少三次独立配对运行；保留全部失败和数据扰动的保留集。
- 总开销包括主模型、筛选、摘要和子代理。任务终态、未核验成功和重复副作用是客观指标，不能被语言质量评分抵消。
- 浏览器、图像、终端、文件、长上下文和中途修正需要各自的受控场景及驱动；仅有 API 场景不能证明这些能力已经达到 Codex 水平。

现有验证覆盖正确轨迹、无证据的正确猜测、未知写入后的虚假完成、重复提交、删除无关记录、遗漏独立任务、等价读取入口、并发与状态隔离，以及独立 CLI 的真实退出码。它验证的是评测基础自身。
