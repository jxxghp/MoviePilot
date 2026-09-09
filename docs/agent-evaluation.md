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

只有显式 `--live` 或 `--native` 才会调用真实模型并产生费用。默认读取 `~/.codex/config.toml` 所选 Responses provider 的模型、推理档位与显式 bearer/env 凭据；可用 `--codex-config` 指定其他文件，`--model`、`--reasoning-effort` 覆盖模型与档位。不会借用其他服务的登录凭据，也不会自动降低被供应商拒绝的参数。报告同时保留请求模型与供应商返回的模型标识；本地 Codex 配置不能证明运行中的 MoviePilot 使用相同配置。

调用配置经私有标准输入传给 worker，凭据不进入命令行、提示词或报告。worker 只继承必要的平台环境，先创建临时 `CONFIG_DIR`，再导入后端；每轮拥有独立回执库、记忆、会话和工具实例。生产 `process/_create_agent`、Skills、计划、权限、持久回执、工具输出预算、压缩和子代理仍按真实路径执行。主工具目录限定为假业务 API 和生产内部工具，API transport 拒绝任何外部目标及场景外 operation；插件、外部 MCP、通知和任意 shell/文件/浏览器工具不开放。此受控目录是当前评测边界，不代表默认部署工具全集。

共享回调在请求前执行硬调用上限，覆盖主模型、选择、摘要和子代理；SDK 自动重试关闭。单请求超时最多 120 秒，全轮和独立进程另有期限。每次请求的输出 token 及运行器上下文上限被记录；上下文上限是测试参数，不表示模型真实最大窗口。当前固定为 128000 tokens。

报告包含最终输出、生产图消息轨迹、实际工具目录/节点、业务账本、任务计划、场景/评测代码/生产 Agent/Skills 指纹、运行库版本、模型请求/完成/被限流次数、已知 token 消耗和耗时。失败请求的用量未知时，`usage_complete=false`，token 仅为已知下界，不能据此声称零消耗。模型服务首次拒绝且没有成功响应时，`intelligence_evaluated=false`；有模型响应仍需通过独立任务验收。`agent_execution_success` 仅表示生产图技术执行结果，不等于任务完成。

`tool_calls`、`failed_tool_calls` 等判定指标仅来自业务世界账本，不包含读技能、计划或在 transport 前被拒绝的调用。`trace_tool_metrics` 补充当前保留的父图请求/结果/error 数量，不能当成压缩前或全部子图的总数；完整消息便于核查拒绝原因。生产提示可能建议查询媒体库等当前假世界未开放的能力，此类拒绝属于受控环境限制，不能据此认定模型调用了无效的生产 API。

单测不调用真实模型或外部网络；测试只验证运行器、隔离、预算与判定合同。真实报告含合成任务轨迹，也可能很长，应保存于评测工作目录；不要把包含私有配置的临时诊断日志提交到仓库。

## 原生 Codex 受控运行

先使用无真实模型请求的目录探针，再运行真实场景：

```bash
uv run --locked --no-sync python -m scripts.evaluation --native-probe \
  --scenario dedup_existing --model gpt-6-astra --reasoning-effort max \
  --output probe.json
uv run --locked --no-sync python -m scripts.evaluation --native \
  --scenario dedup_existing --model gpt-6-astra --reasoning-effort max \
  --max-model-calls 12 --timeout-seconds 180 --output codex-report.json
```

适配器目前只核对 `codex-cli 0.153.4`，可用 `--codex-executable` 指定文件。其他版本或二进制目录中没有指定模型时明确拒绝，不换模型或套用其他模型的元数据。`--native-probe` 的 `probe_ready` 只代表配置和目录检查，不是任务通过；原生进程预期收到本地探针错误并退出，模型调用数仍为零。

每次创建空白临时工作目录；场景源码、初态、账本、oracle、模型凭据留在控制器。原生客户端使用局部随机令牌连接回环模型代理和 MCP 假世界，不继承业务配置、真实模型令牌或其他服务环境。客户端忽略用户配置和规则文件，关闭宿主技能、插件、浏览器、文件查看及 shell 等能力，采用只读沙箱、never 审批与有界退出；仅对当前回环 evaluation 服务的三个假工具显式设置 `approval_mode=approve`，避免原生客户端拒绝已授权的假业务动作。不修改用户 HOME、CODEX_HOME 或现有配置。全局 AGENTS 仍可能由原生客户端加载，因此代理在发送给模型前仅移除规范的独立 AGENTS 用户块，保留原生基础说明和任务文本，并记录被移除块的长度与哈希。

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
