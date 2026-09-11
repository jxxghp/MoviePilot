# MoviePilot Agent 与 Codex Harness 对齐路线

> 归档位置：`docs/refactor/`。

更新时间：2026-09-11

这份文档是 Agent 能力对齐的交付路线和每轮验收合同。它记录当前证据与未完成目标，不把一次成功的模型调用当成“已经和 Codex 一样聪明”。最终判断必须同时看模型行为、工具执行、任务生命周期、业务终态和失败后的收敛结果。

## 当前目标

让 MoviePilot Agent 在 MoviePilot 的真实业务边界内具备可复核的 Codex 级 Harness 能力：模型能看到准确的工具合同，工具能完成完整的命令行、浏览器、终端输入输出、子代理和长任务协作闭环，宿主能隔离任务身份、收口进程并在新消息到达时继续推进。高影响业务动作仍由宿主的授权和确认策略控制。

真实模型评测必须使用显式配置的供应商模型和推理档位。历史 Google Gemini 基线为 `gemini-2.5-pro + high`；后续测评切换为更强的 `gemini-3.1-pro-preview + high`。官方 Google 主机由评测 worker 使用生产同源的 `langchain-google-genai` 原生工具通道，以保留 Gemini 3 的 `thought_signature`；报告会把实际 `runtime_transport` 记为 `google_generative_language`。不同模型、供应商或协议的结果不能直接合并；首个同条件原生 Codex 配对样本未同时通过，随后同 Harness 的 `dedup_existing` 和 `unknown_download` 配对已同时通过，`honest_unknown` 仍保留原生侧预算耗尽失败。

Google 的 OpenAI-compatible 层会丢失 Gemini 3 工具回复中的 `thought_signature`，第二轮请求因此可能被供应商以 400 拒绝。真实 MoviePilot Agent 的 `runtime == "google"` 已固定走 `ChatGoogleGenerativeAI` 和签名兼容补丁；需要修正的是评测或错误的 provider 配置，不能通过普通 OpenAI 消息层补回签名。该边界由 `tests/test_agent_vision_providers.py`、`tests/test_llm_helper_testcall.py` 和本轮真实原生 Google 运行共同覆盖。

## 交付顺序

| 目标 | 状态 | 验收重点 |
| --- | --- | --- |
| C1.4 终端工具完整闭环 | PTY 已配对；原生 pipe stdin 仍受边界阻塞 | `run/pipe/PTY` 共用 shell、cwd、login 和 UTF-8 策略；stdin 写入、EOF、分页、interrupt/kill、超时和进程组收尾有真实进程测试。原生终端场景经由 app-server 采集 `outputDelta`/`terminalInteraction`，并按评测预算保留足够的流式空闲时间；PTY 配对已观察输入、输出和退出码，pipe 配对仍保留原生 CLI 关闭 stdin、未产生 `terminalInteraction` 的失败证据 |
| C1.3 终端任务作用域 | 已完成 | 终端归属由宿主对象身份决定；定时运行、会话、子任务和内部工具管理器隔离；封口先于清理，排队或运行中的命令都不会在任务结束后迟到启动 |
| S2.3 运行中消息排队与 WebAgent 输入 | 已完成本轮实现并修复丢消息、时序错位和 continuation 状态不刷新的遗漏 | 运行中仍可提交新消息；消息按会话原子入队，在下一次模型调用边界注入真实 `HumanMessage`；SSE 报告 queued/applied，稳定 `steering_message_id` 贯穿展示快照、前端本地状态和恢复合并；应用点收口当前助手并创建 continuation 助手，用户气泡落在真实事件边界；工具按 `tool_id` 报告 running/done/error；停止后不再派发后续工具 |
| C1.5 长上下文压缩与任务约束保留 | 同条件配对已通过 | 真实压缩前后预算约 69.5K → 26.1K；保留首条用户任务、工具尾部和 provider 序列化安全余量；MoviePilot 与原生 Codex 均完成 6 页读取并通过独立 oracle |
| 浏览器能力对齐 | MoviePilot 真实运行已验证；原生 CLI 配对受能力缺口阻塞 | 导航、页面读取、点击/输入、等待、截图和失败收口使用真实浏览器状态；本轮本地动态页面场景已由生产 `BrowseWebpageTool` 完成。`codex features list` 虽显示 `browser_use` 为 stable，但真实受控 `codex exec 0.153.4` 请求仍只保留计划、工具搜索和评测 MCP；浏览器场景 12 次模型调用后没有产生浏览器账本事件并耗尽预算，feature flag 不能替代原生 browser plugin/host，保留为 Harness 能力缺口，不能伪造配对 |
| S3.1 通用子代理 | 三轮 held-out 配对通过，仍需终端分享/取消边界收益数据 | 主 Agent 只暴露并派发 `general-purpose`；旧的专用画像已删除，不保留兼容入口。held-out 场景已在三轮独立运行中验证两个并行只读子任务、授权、工具角色和零副作用；仍需终端分享/取消边界的收益数据 |

## 每轮硬门禁

每个影响 Agent、工具、会话、浏览器、子代理或提示词的代码轮次都要执行下面四层检查，并把结果与最终提交 SHA 绑定：

1. **确定性回归**：运行受影响的 pytest、全量 Agent 测试、类型/格式/架构 ratchet 和必要的全量测试。任何已知基线失败都要在未改动基线复现后再归因。
2. **真实 MoviePilot 运行**：使用评测配置中显式声明的供应商模型和支持档位（本轮 Google 为 `gemini-3.1-pro-preview` + `high`；官方 Codex OAuth 配对可显式使用 `gpt-5.6-luna` + 配置档位），在隔离 worker 中跑固定场景，保留真实模型请求、工具轨迹、耗时、token、终态和进程收尾证据。业务副作用只能进入评测假世界，不能把测试凭据或私有响应写入报告。
3. **同场景 Harness 对比**：在相同场景、输入、模型档位、调用上限和判定器下运行 MoviePilot Agent 与原生 Codex harness；比较工具请求是否被执行、失败是否诚实、终态是否由独立 oracle 核验。原生 harness 不可用时报告 `blocked`，不能以离线脚本或单元测试替代。
4. **留存与复核**：报告目录使用 `evidence/agent-round/<commit-sha>/`（不提交凭据），至少包含 `model`, `reasoning_effort`, `scenario`, `agent_sha`, `harness_sha`, `model_calls`, `tokens`, `elapsed_seconds`, `tool_trace`, `business_oracle`, `process_oracle` 和失败分类。重复运行同一轮时保留每次报告，不能覆盖异常样本。

当前评测入口：

```bash
UV_PROJECT_ENVIRONMENT=/Users/jxxghp/MPProjects/MoviePilot/.venv \
uv run --locked --no-sync python -m scripts.evaluation \
  --live --scenario unknown_download \
  --model gemini-3.1-pro-preview --reasoning-effort high \
  --output evidence/agent-round/<commit-sha>/moviepilot-unknown_download.json
```

同一场景的原生 harness 使用 `--native`，报告必须和 `--live` 放在同一个提交目录下。真实评测不能在 pytest 中隐式联网；命令失败、模型拒绝、token 不完整或 oracle 未核验都标记为未完成，不得记为通过。

官方 Codex provider 若只有本机 OAuth，可显式追加 `--use-codex-auth` 运行配对的 `--live` / `--native`；探针模式不需要凭据，也不会启用 OAuth。

## 工具与 Harness 对齐清单

- **命令行**：普通一次性命令与后台终端分别支持 pipe/PTY、共享 cwd/shell/login/环境和 UTF-8；输入写入、空写入、EOF、分页、短写、interrupt、kill、超时、取消和进程组收尾都有结构化终态。`command_execution` 与终端场景只在原生 CLI 真实广告 `functions.exec_command`/`functions.write_stdin` 时按场景开启，并把 MoviePilot 生产 `ExecuteCommandTool` 绑定到临时目录；终端场景的原生评测使用 app-server，记录命令输出增量和终端输入事件。生产与原生 PTY 已通过同条件真实配对；原生 pipe 仍因 CLI 在一次性命令后关闭 stdin 而没有输入事件，保持 blocked，不把适配器能力误算成原生 Harness 对齐。适配器将单请求、代理和 app-server 流式空闲时间统一跟随评测墙钟预算，避免较长推理被 Harness 的硬编码超时误判。
- **浏览器**：工具目录必须明确导航、读取、交互、等待和截图的动作与权限；浏览器会话、页面状态和失败重试由宿主持有，不能由模型字符串冒领。新增 `browser_navigation` 场景使用回环动态页面验证生产工具；原生 CLI 探针没有广告浏览器工具，原生配对保持 blocked，等待可用的 Codex browser harness。
- **子代理**：主 Agent 自动选择通用子代理；专用画像只有在 held-out 任务上证明提高成功率、减少调用或降低副作用风险时才保留。子代理不能自行发送消息、执行高影响写操作或继承兄弟任务句柄。
- **API Skill 与合同读取**：`skills/moviepilot-api/SKILL.md` 只负责路由索引和工作流；`api/*.md` 各自包含完整操作合同及该类别需要的 Body Models，不再依赖单独的 `api/models.md`。真实 Agent 先用 `read_skill` 得到 supporting-file 清单，再用同一个工具的 `file=api/<category>.md` 参数按需读取类别文档；评测目录必须提供同一条链路，不能只返回文件名而不给模型读取能力。operation 输入错误还要返回该 operation 的允许字段、必填字段和类型约束，帮助模型纠正后重试。
- **长任务与新消息**：入站消息在任务运行时仍可接受并进入有界队列；应用到下一模型边界时必须保留 tool-call/tool-result 配对和取消语义，不能只把文本拼到系统提示词。WebAgent 将工具开始/完成/失败作为带稳定 `tool_id` 的结构化事件；前端按事件序列维护每个工具状态，queued ACK 只建立排队气泡，后续事件继续归属于当前助手段，直到 applied 事件报告真实模型边界后再创建 continuation，避免“最新工具覆盖全部状态”或把用户消息挪到顶部。
- **观察证据**：最终答案只能引用宿主记录的工具结果和业务 oracle；“模型说完成”不能替代数据库、下载器、浏览器或进程状态核验。

## 通过标准与未完成边界

“对齐”至少需要多个固定场景、多个重复轮次和一组未见场景在同一模型档位下持续通过，并且命令行、浏览器、消息排队、子代理和取消路径都具备失败证据。一次 live pilot、单个场景或单元测试通过只能证明局部合同成立，不能证明与 Codex 的整体智能相等。

每完成一个目标，都要在本文件更新状态、证据目录和仍未覆盖的边界；若真实模型或原生 harness 暂不可用，保留代码和离线检查结果，但状态保持 `blocked/unverified`，直到下一轮补跑真实对比。

## 2026-09-11 实测记录

- `a2f40ceec`：评测目录首次加入生产 `read_file`，发现临时 Agent 根与通用 `CONFIG_PATH/agent` 的权限根不一致。
- `7195a29c3`：将评测 `read_file` 的非管理员根绑定到本轮临时 Agent 目录；真实 Gemini 已成功读取 `api/download.md` 和 `api/site.md`。
- `8c0746172`：在 `api/download.md` 直接补充 `download.add` 的最小 `torrent_in` 结构并升到 Skill v30。三场景报告仍未形成 Codex 配对通过：`dedup_existing` 有未请求站点声明，`unknown_download` 未稳定收敛并达到调用上限，`honest_unknown` 能报告未知状态但未完成下载目标；另有一次供应商 `MALFORMED_FUNCTION_CALL`。
- `a731cc0ce`：为评测世界加入生产 allowlist 中的 `library.exists` 只读操作，并以同一 Gemini 配置重跑三场景。`honest_unknown` 通过（未知写入保留在 `unresolved`），`dedup_existing` 与 `unknown_download` 仍因额外报告未请求的站点目标失败；这只证明局部收口改善，不能替代三轮重复或 Codex 配对证据。
- `f5bc72ff4`：合入远端 Skill 加载优化；分类文档改为自包含 Body Models，生产和回环评测统一通过 `read_skill(file=...)` 读取列出的辅助文件，不再开放 `read_file` 绕过 Skill 边界。
- `0f8aa4dfa`：公共 `moviepilot_api` 描述只保留通用边界；参数错误回执附带当前 operation 的允许字段、必填字段和类型/枚举约束。真实 Gemini `dedup_existing` 报告 `/tmp/moviepilot-agent-round-13b4fe112-gemini-dedup.json` 证明错误回执能促成 `subscription.find` 字段位置修正且没有副作用，但模型仍错误声明未请求的站点和已完成下载，继续保持未通过。
- 本轮：真实测评切换到 `gemini-3.1-pro-preview + high`，官方 Google 主机改用生产同源的 `langchain-google-genai` 原生工具通道并保留 `thought_signature`。`dedup_existing`、`unknown_download`、`honest_unknown` 三个固定场景均通过（报告分别为 `/tmp/moviepilot-agent-round-gemini31native-dedup.json`、`/tmp/moviepilot-agent-round-gemini31native-unknown.json`、`/tmp/moviepilot-agent-round-gemini31native-honest.json`）；这只是 API 假世界的模型行为证据，仍未形成与原生 Codex 的配对比较。
- 本轮第二阶段：用同一 `gpt-5.6-luna + max`、同一场景和预算显式启用 Codex OAuth，首次生成有效成对摘要 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair.json`。MoviePilot live `/tmp/moviepilot-agent-round-luna-oauth-live-dedup-5.json` 因最终 infohash 少两位而未通过；原生 Codex `/tmp/moviepilot-agent-round-luna-oauth-native-dedup-8.json` 通过。摘要 `pair_valid=true`、`both_passed=false`，证明真实生产图与 Codex 仍有可观测的终态可靠性差距；下一轮先针对精确最终报告和未知结果收口做多次重复，再扩展浏览器、终端和中途消息场景。
- 本轮第三阶段：修正原生动态工具搜索对 `multi_agent_v1` 协作命名空间的投影，正向保留 `wait_agent`、`resume_agent` 等原生控制动作，负向继续拒绝 `read_file`。在同一 Harness（`71be4c436acc5dc2d5a0cd6996c3ca10688a9c4ba98fd11594e536f7aa440c22`）下，`dedup_existing` 配对 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-13.json` 与 `unknown_download` 配对 `/tmp/moviepilot-agent-round-luna-oauth-unknown-pair-14.json` 均 `pair_valid=true`、`both_passed=true`；MoviePilot 分别使用 6/6 次模型调用，原生 Codex 使用 10/14 次。`honest_unknown` 配对 `/tmp/moviepilot-agent-round-luna-oauth-honest-pair-16.json` 仍为 `both_passed=false`：MoviePilot 在 9 次调用后如实保留下载未核验，原生 Codex 达到 16 次上限后没有最终 JSON，产生 `invalid_final_report`。这保留了真实失败边界，不能等同整体 Codex 智能已完成。
- 命令行与浏览器能力阶段：`command_execution` 在同一 `gpt-5.6-luna + max` 和独立 Codex OAuth 下已形成真实配对 `/tmp/moviepilot-agent-round-luna-oauth-command-pair-final.json`，`pair_valid=true`、`both_passed=true`，harness 为 `c409ee1ca7899ccfcefd38832d6a343f024f8fc67fb65f14c3eec08552203257`。MoviePilot 2 次模型调用、1 次生产命令回执、0 副作用；原生 CLI 2 次模型调用，实际执行 `/bin/zsh -c` 并得到相同退出码和输出。生产 stdout 的 `[标准输出]` 展示包装已在独立判定器中与命令内容分离。
- 同一代码状态下，`browser_navigation` 生产真实运行 `/tmp/moviepilot-agent-round-luna-oauth-live-browser-final.json` 通过：6 次模型调用、4 次浏览器回执，真实页面点击后观察到 `BROWSER_OK`。`codex exec 0.153.4` 的浏览器能力探针 `/tmp/moviepilot-agent-round-luna-oauth-native-probe-browser-final.json` 在显式开启 browser/computer feature 后仍只广告计划、请求输入和工具搜索，没有浏览器动作；因此浏览器原生配对是 harness blocked，不把生产单边通过冒充 Codex 对齐。
- `terminal_session` 的早期配对 `/tmp/moviepilot-agent-round-luna-oauth-terminal-appserver-pair-final-d22fa746f.json` 保留了原生 pipe 在 READY 前关闭 stdin 的失败证据。随后评测适配器改用与整轮预算一致的流式超时，仍不掩盖该一次性 pipe 交互边界；生产路径的持续会话测试保持通过。
- 同条件的 `terminal_pty_session` 配对 `/tmp/moviepilot-agent-round-luna-oauth-terminal-pty-appserver-pair-timeout180-sleep1-ready1-20260911.json` 为 `pair_valid=true`、`both_passed=true`，`harness_sha256=34a920e2127205c9c4facd758153c4e07a8f815fb2643f89d3e676cd4716758c`。MoviePilot `/tmp/moviepilot-agent-round-luna-oauth-live-terminal-pty-timeout180-sleep1-ready1-20260911.json` 4 次模型调用、3 次真实终端操作；原生 app-server `/tmp/moviepilot-agent-round-luna-oauth-native-terminal-pty-appserver-timeout180-sleep1-ready1-20260911.json` 3 次模型调用，均由独立账本确认 PTY 输入、`READY`/回复输出和退出码 0。PTY 探针命令在输出 READY 前保留 1 秒启动窗口、回复后保留 1 秒收尾窗口，模拟真实服务启动和终端交接，避免丢失进程早期输出。
- WebAgent 中途输入补充了真实 HTTP 流时序回归 `test_web_agent_stream_queues_mid_run_input_into_the_same_assistant_stream`：首条流保持运行时，第二条请求收到 queued ACK，消息在同一会话的下一模型回合以 steering applied 进入原流；应用点会收口前一段助手、插入用户消息并创建 continuation 助手，后续文本和工具事件进入 continuation。该回归验证了宿主排队和展示边界；真实模型长任务配对仍待补跑。
- 本轮 WebAgent 留存与工具状态修复了两类真实时序：后端展示快照和 `AgentChatMessage` 记录 `steering_message_id`，前端持久化 queued 消息并在服务端恢复快照、刷新和 ACK 断流时合并保留，迟到 applied 事件更新同一条用户气泡；工具事件携带稳定 `tool_id` 与 `running/done/error` 状态，前端不再用最后一条提示覆盖之前的完成态。后端相关测试 162 项和前端 `AgentAssistantPanel.spec.ts` 41 项通过，`vue-tsc --noEmit` 通过。
- 追加验证发现 continuation 助手插入后若把原始对象直接存入 steering 映射，后续工具状态会写入但不会触发 Vue 重渲染；前端提交 `44e19778b70fd17bc0a7ddf63021866ef45bdaa5` 改为从响应式消息数组取回 continuation，并增加“工具 1 完成 → 插入用户消息 → 工具 2 running”的时序回归。前端 `AgentAssistantPanel.spec.ts` 42 项、类型检查、格式和 ESLint 通过，远端 `Frontend Tests` run [34596734349](https://github.com/jxxghp/MoviePilot-Frontend/actions/runs/34596734349) 成功。
- 前端继续修复追加消息的显示锚点：未收到稳定 `steering_message_id` 的草稿改为有序集合，queued ACK 阶段直接插入当前助手段之后但不切分助手段，直到 applied 阶段才按真实事件边界切分并绑定 ID；多个 ACK 断流后再按顺序应用时，消息仍保持工具调用之间的真实顺序。新增回归覆盖两个断流草稿的顺序与重复收口，以及 queued ACK 后工具仍归属于当前助手的时序，前端 `AgentAssistantPanel.spec.ts` 44 项、类型检查、格式和 ESLint 通过。
- 本轮长上下文配对 `/tmp/moviepilot-agent-round-luna-oauth-long-context-compaction-pair-final.json` 使用同一 `gpt-5.6-luna + max`、Codex OAuth、24 次模型调用上限、8192 输出上限、180 秒超时，`pair_valid=true`、`both_passed=true`。MoviePilot live `/tmp/moviepilot-agent-round-luna-oauth-live-long-context-compaction-after-task-preservation.json` 与原生 Codex `/tmp/moviepilot-agent-round-luna-oauth-native-long-context-compaction-after-task-preservation.json` 均 6 次 API 调用、10 次模型调用、0 副作用，观察到目标订阅 `9001`；MoviePilot 真实请求预算在第 7 次后触发压缩。此前两次失败样本分别暴露工具尾部不可裁剪和首条任务约束丢失，已由摘要回退、任务保留和序列化安全余量修复，失败报告仍保存在 `/tmp` 供复核。
- 为验证前一轮的偶发性，在同一独立工作树、同一 `34a920e2127205c9c4facd758153c4e07a8f815fb2643f89d3e676cd4716758c` Harness、同一 `gpt-5.6-luna + max` 和 16 次调用预算下重复 `dedup_existing` 两轮：第二轮成对摘要 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-repeat2-worktree-20260911.json` 为 `pair_valid=true`、`both_passed=true`（MoviePilot 7 次模型调用/131491 tokens，Codex 10 次/220335 tokens，均 0 重复/副作用）；第三轮 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-repeat3-worktree-20260911.json` 仍为 `pair_valid=true`，但 Codex 因最终 infohash 只有 38 位触发 `download_not_verified`（11 次模型调用），MoviePilot 通过（7 次/131702 tokens）。连同同 Harness 的第一轮 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-repeat1-20260911.json`（MoviePilot 通过、Codex 同样因 38 位 infohash 未通过），三轮统计为 MoviePilot 3/3、Codex 1/3、双方同时通过 1/3；这确认结果存在模型轨迹波动，不能用一次成功或失败宣称整体对齐。一次误在其他 checkout 生成的原生报告因 Harness SHA 为 `eee4b6ad...` 被 `--compare` 拒绝，证明配对指纹控制有效。
- 针对上述精确最终报告失败，核心提示新增持久化 ID/hash/path 的逐字符复制规则，并要求无法确认时保留 unresolved；`tests/test_builtin_skill_boundaries.py` 聚焦 11 项通过。相同未提交工作树、`gpt-5.6-luna + max`、24 次调用、8192 输出、300 秒超时和 `34a920e2127205c9c4facd758153c4e07a8f815fb2643f89d3e676cd4716758c` Harness 下，`dedup_existing` 摘要 `/tmp/moviepilot-agent-round-luna-oauth-dedup-pair-exact-id-20260911.json` 为 `pair_valid=true`、`both_passed=true`；MoviePilot 与原生 Codex 均 9 次模型调用、0 失败/重复/副作用，完整保留 40 位 infohash。仍需用多轮固定场景和 held-out 任务验证该规则的稳定收益。
- held-out 子代理场景首次运行发现 `general-purpose` 子代理的 `subscription.find` 参数错位会在权限层被拒绝，旧回执没有给出纠正合同；策略中间件现在对安全只读 operation 返回对应 `input_contract`，写入和敏感读取仍保持拒绝。相关权限、评测和场景测试通过。`subagent_parallel_status` 在同一 `gpt-5.6-luna + max`、32 次调用、8192 输出、300 秒超时和 `harness_sha256=471c2910242d322455c2b7a825876aef10965a51bf7472ea2a7f93f25adaf04d` 下，第一轮 MoviePilot `/tmp/moviepilot-agent-round-luna-oauth-subagent-live-32-20260911.json` 使用 13 次模型调用、2 次业务读取通过；原生 Codex `/tmp/moviepilot-agent-round-luna-oauth-subagent-native-32-20260911.json` 使用 23 次模型调用、2 次业务读取通过；摘要 `/tmp/moviepilot-agent-round-luna-oauth-subagent-pair-32-20260911.json` 为 `pair_valid=true`、`both_passed=true`，两侧均真实派发两个独立子任务并保持零副作用。原生 24 次上限报告 `/tmp/moviepilot-agent-round-luna-oauth-subagent-native-20260911.json` 因最终 JSON 前预算耗尽保留为失败样本；第一轮原生 stderr 记录一次关闭协作任务的参数警告，未影响业务终态。
- 同一提交内容和 Harness 的第二轮独立运行继续通过：MoviePilot `/tmp/moviepilot-agent-round-luna-oauth-subagent-live-32-repeat2-20260911.json` 使用 11 次模型调用、63425 个已知 token、2 次业务读取；原生 Codex `/tmp/moviepilot-agent-round-luna-oauth-subagent-native-32-repeat2-20260911.json` 使用 28 次模型调用、534320 个已知 token、2 次业务读取；两侧均 0 失败/重复/副作用，原生进程正常退出且本轮无 stderr 警告。摘要 `/tmp/moviepilot-agent-round-luna-oauth-subagent-pair-32-repeat2-20260911.json` 为 `pair_valid=true`、`both_passed=true`。两轮后的证据把 S3.1 从单轮样本提升为可重复通过样本，但仍不能外推整体智能等价或终端分享/取消收益。
- 第三轮在相同提交内容、模型、推理档位、预算和 Harness 下继续通过：MoviePilot `/tmp/moviepilot-agent-round-luna-oauth-subagent-live-32-repeat3-20260911.json` 使用 11 次模型调用、54388 个已知 token、2 次业务读取；原生 Codex `/tmp/moviepilot-agent-round-luna-oauth-subagent-native-32-repeat3-20260911.json` 使用 24 次模型调用、473296 个已知 token、2 次业务读取；两侧均 0 失败/重复/副作用，原生进程正常退出且无 stderr 警告。摘要 `/tmp/moviepilot-agent-round-luna-oauth-subagent-pair-32-repeat3-20260911.json` 为 `pair_valid=true`、`both_passed=true`。三轮通过满足该 held-out 场景的重复配对门槛，但仍不能外推整体智能等价或终端分享/取消收益。
