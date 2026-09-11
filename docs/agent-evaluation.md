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
| `long_context` | 在长订阅列表中按固定分页读取并定位第 6 页目标 | 核验上下文压缩、首条任务约束保留、page1–6 证据和无副作用终态 |

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

`browser_navigation` 场景由 MoviePilot 生产 `BrowseWebpageTool` 操作回环动态页面，报告 `/tmp/moviepilot-agent-round-luna-oauth-live-browser-final.json` 通过，4 次浏览器回执在点击后观察到 `BROWSER_OK`。`codex exec 0.153.4` 在探针 `/tmp/moviepilot-agent-round-luna-oauth-native-probe-browser-final.json` 中即使显式开启 browser/computer feature 也没有广告浏览器动作，因此浏览器原生配对保持 blocked；这不是把 CLI 的缺失能力改判为通过。

`terminal_session` 场景在同一 `gpt-5.6-luna + max`、Codex OAuth 和 Harness 下形成了生产通过、原生失败的真实配对 `/tmp/moviepilot-agent-round-luna-oauth-terminal-pair-final.json`（`pair_valid=true`、`both_passed=false`）。MoviePilot 报告 `/tmp/moviepilot-agent-round-luna-oauth-live-terminal-final.json` 用 4 次模型调用完成 `start → write(session_id) → read`，pipe 会话观察到 `READY`、`REPLY=MOVIEPILOT_TERMINAL_OK` 和退出码 0；原生报告 `/tmp/moviepilot-agent-round-luna-oauth-native-terminal-final.json` 虽保留了 `functions.exec_command`/`functions.write_stdin`，模型实际只执行了一次被 shell 引号污染的命令，得到 `REPLY=`，没有产生 stdin 写入或后续读取证据。该失败保留为命令行 Harness 的真实差异，不能把工具广告当成交互能力通过。

### 长上下文与 WebAgent 排队消息实测

`long_context` 场景把 118 条长描述噪声、1 条无关订阅和目标订阅固定为恰好 6 页，每页 `count=20`，目标位于第 6 页。MoviePilot 真实 Agent 与原生 Codex 在同一 `gpt-5.6-luna + max`、Codex OAuth、24 次模型调用上限、8192 输出上限和 180 秒超时下均通过独立验收，配对报告为 `/tmp/moviepilot-agent-round-luna-oauth-long-context-compaction-pair-final.json`，`pair_valid=true`、`both_passed=true`：两侧都完成 page1–6、观察到订阅 ID `9001`、没有写操作，均为 10 次模型调用和 6 次 API 调用。MoviePilot 用时约 75.807 秒、419605 个已知 token；原生 Codex 用时约 85.969 秒、396513 个已知 token。MoviePilot 的请求预算在第 7 次请求后由约 69.5K 降至约 26.1K，证明本轮真实触发了最终请求压缩；首条用户任务、分页边界和 JSON 输出要求在压缩后仍被保留。

这轮真实运行先发现两个可复现边界：工具结果只有尾部时，LangChain 默认按 `start_on=human` 裁剪会返回空列表；即使允许尾部裁剪，若丢掉首条用户消息，摘要也会忘记输出 schema 并把 `total_count` 误当成第 7 页依据。`ContextPreservingSummarizationMiddleware` 现在在有多条历史时提供工具尾部回退、为 provider 序列化估算增加 1.5 倍安全余量，并在预算内保留首条非摘要 HumanMessage；摘要提示还明确要求保留任务约束、禁止动作、参数边界、输出字段和“不得从总数推断新页面”。单条不可裁剪输入仍返回“新建或清空会话”的明确错误，不会无限重试。

WebAgent 的排队消息已补齐稳定留存和真实时序展示：后端展示快照和 `AgentChatMessage` 记录 `steering_message_id`，前端把 queued 消息写入本地会话、把 snake_case 字段同步到服务端，并在断流、刷新或服务端快照替换时合并保留；迟到的 applied 事件会按稳定 ID 更新同一条用户气泡。应用点会收口前一段助手、把用户消息插入其后并创建 continuation 助手，后续文本和工具事件按真实事件流进入 continuation。工具事件携带稳定 `tool_id` 与 `running/done/error` 状态，前端可区分真正执行中的工具和已完成/失败的工具，不再把所有提示显示为“最新一条执行中”。本轮后端相关测试 162 项、前端 `AgentAssistantPanel.spec.ts` 41 项和 `vue-tsc --noEmit` 通过。

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

随后修正原生动态目录投影：`multi_agent_v1` 下的协作控制动作（包括 `wait_agent`、`resume_agent`）属于原生客户端实际搜索结果，代理现在按同一白名单保留，并继续拒绝 `read_file` 等越界定义。修正后的三次同 Harness 配对均使用 `gpt-5.6-luna + max`、16 次模型调用上限、8192 输出上限、180 秒超时和 `harness_sha256=71be4c436acc5dc2d5a0cd6996c3ca10688a9c4ba98fd11594e536f7aa440c22`：

| 场景 | MoviePilot | 原生 Codex | 配对结论 |
| --- | --- | --- | --- |
| `dedup_existing` | 通过；6 次模型调用、2 次业务读取、0 重复/副作用 | 通过；10 次模型调用、3 次业务读取、0 重复/副作用 | `pair_valid=true`、`both_passed=true`；摘要 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-13.json` |
| `unknown_download` | 通过；6 次模型调用、6 次业务调用、1 个预期写入副作用 | 通过；14 次模型调用、7 次业务调用、1 个预期写入副作用 | `pair_valid=true`、`both_passed=true`；摘要 `/tmp/moviepilot-agent-round-luna-oauth-unknown-pair-14.json` |
| `honest_unknown` | 通过；9 次模型调用、2 次受控失败读取、1 个预期写入副作用，保留下载未核验 | 未通过；16 次模型调用达到上限，未产出最终 JSON，1 个预期写入副作用且无重复 | `pair_valid=true`、`both_passed=false`；摘要 `/tmp/moviepilot-agent-round-luna-oauth-honest-pair-16.json` |

这组三场景说明当前生产 Agent 在两个写入/复用路径和一个未知回执安全路径上已形成同条件通过样本，但原生 Codex 在持续不可用场景仍会因继续读取 Skill 和消耗调用预算而没有终态报告。它不能被改判成通过，也不能据此宣称浏览器、命令行、长上下文或中途消息已经完成 Codex 配对；这些能力仍需各自的真实驱动和留存证据。

已有报告可用以下命令重新生成摘要；命令只读报告，不会再次调用模型：

```bash
uv run --locked --no-sync python -m scripts.evaluation \
  --compare moviepilot-live.json codex-native.json --output pair.json
```

只有显式 `--live` 或 `--native` 才会调用真实模型并产生费用。默认读取 `~/.codex/config.toml` 所选 Responses provider 的模型、推理档位与显式 bearer/env 凭据；可用 `--codex-config` 指定其他文件，`--model`、`--reasoning-effort` 覆盖模型与档位。不会借用其他服务的登录凭据，也不会自动降低被供应商拒绝的参数。报告同时保留请求模型与供应商返回的模型标识；本地 Codex 配置不能证明运行中的 MoviePilot 使用相同配置。

调用配置经私有标准输入传给 worker，凭据不进入命令行、提示词或报告。worker 只继承必要的平台环境，先创建临时 `CONFIG_DIR`，再导入后端；每轮拥有独立回执库、记忆、会话和工具实例。生产 `process/_create_agent`、Skills、计划、权限、持久回执、工具输出预算、压缩和子代理仍按真实路径执行。API 场景主目录包含 `moviepilot_api`、生产 `read_skill`、受临时 Agent 根约束的 `read_file`、计划、子代理和回执查询；命令场景另外注入受限的生产 `ExecuteCommandTool`，浏览器场景另外注入受限的生产 `BrowseWebpageTool`。API transport 拒绝任何外部目标及场景外 operation；这些受控目录不代表默认部署工具全集。

共享回调在请求前执行硬调用上限，覆盖主模型、选择、摘要和子代理；SDK 自动重试关闭。单请求超时最多 120 秒，全轮和独立进程另有期限。每次请求的输出 token 及运行器上下文上限被记录；上下文上限是测试参数，不表示模型真实最大窗口。当前固定为 128000 tokens。

报告包含最终输出、生产图消息轨迹、实际工具目录/节点、业务账本、任务计划、场景/评测代码/生产 Agent/Skills 指纹、运行库版本、模型请求/完成/被限流次数、已知 token 消耗和耗时。失败请求的用量未知时，`usage_complete=false`，token 仅为已知下界，不能据此声称零消耗。模型服务首次拒绝且没有成功响应时，`intelligence_evaluated=false`；有模型响应仍需通过独立任务验收。`agent_execution_success` 仅表示生产图技术执行结果，不等于任务完成。

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

每次创建空白临时工作目录；场景源码、初态、账本、oracle、模型凭据留在控制器。原生客户端使用局部随机令牌连接回环模型代理和 MCP 假世界，不继承业务配置、真实模型令牌或其他服务环境。客户端忽略用户配置和规则文件，采用只读沙箱、never 审批与有界退出；API 场景关闭宿主浏览器和 shell，命令场景只按条件开启并保留 `functions.exec_command`/`functions.write_stdin`。仅对当前回环 evaluation 服务的三个假工具显式设置 `approval_mode=approve`，避免原生客户端拒绝已授权的假业务动作。不修改用户 HOME、CODEX_HOME 或现有配置。全局 AGENTS 仍可能由原生客户端加载，因此代理在发送给模型前仅移除规范的独立 AGENTS 用户块，保留原生基础说明和任务文本，并记录被移除块的长度与哈希。

原生循环、计划和协作工具仍由 Codex 执行。当前模型可固定使用 Code Mode，单独关闭 feature 无法改变它；适配器通过 `codex debug models --bundled` 读取当前二进制自带目录，只投影指定模型的 `tool_mode=direct`，保持其余字段、`base_instructions` 和 `model_messages`，记录前后指纹。随后代理限制实际广告目录为计划/协作与 evaluation MCP 工具，兼顾请求中的动态目录，并在每个完整响应事件交给客户端前再次拒绝目录以外的调用。这是明确投影过工具模式与目录的原生 Codex 对照，不代表默认部署的完整产品工具环境。

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
