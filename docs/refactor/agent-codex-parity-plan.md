# MoviePilot Agent 与 Codex Harness 对齐路线

> 归档位置：`docs/refactor/`。

更新时间：2026-09-11

这份文档是 Agent 能力对齐的交付路线和每轮验收合同。它记录当前证据与未完成目标，不把一次成功的模型调用当成“已经和 Codex 一样聪明”。最终判断必须同时看模型行为、工具执行、任务生命周期、业务终态和失败后的收敛结果。

## 当前目标

让 MoviePilot Agent 在 MoviePilot 的真实业务边界内具备可复核的 Codex 级 Harness 能力：模型能看到准确的工具合同，工具能完成完整的命令行、浏览器、终端输入输出、子代理和长任务协作闭环，宿主能隔离任务身份、收口进程并在新消息到达时继续推进。高影响业务动作仍由宿主的授权和确认策略控制。

真实模型评测必须使用显式配置的供应商模型和推理档位。历史 Google Gemini 基线为 `gemini-2.5-pro + high`；后续测评切换为更强的 `gemini-3.1-pro-preview + high`。官方 Google 主机由评测 worker 使用生产同源的 `langchain-google-genai` 原生工具通道，以保留 Gemini 3 的 `thought_signature`；报告会把实际 `runtime_transport` 记为 `google_generative_language`。不同模型、供应商或协议的结果不能直接合并；当前原生 Codex 尚未取得同一模型与档位的可用配对样本。

## 交付顺序

| 目标 | 状态 | 验收重点 |
| --- | --- | --- |
| C1.4 终端工具完整闭环 | 已完成基线 | `run/pipe/PTY` 共用 shell、cwd、login 和 UTF-8 策略；stdin 写入、EOF、分页、interrupt/kill、超时和进程组收尾有真实进程测试 |
| C1.3 终端任务作用域 | 已完成 | 终端归属由宿主对象身份决定；定时运行、会话、子任务和内部工具管理器隔离；封口先于清理，排队或运行中的命令都不会在任务结束后迟到启动 |
| S2.3 运行中消息排队与 WebAgent 输入 | 已完成本轮实现 | 运行中仍可提交新消息；消息按会话原子入队，在下一次模型调用边界注入真实 `HumanMessage`；SSE 报告 queued/applied，停止后不再派发后续工具；剩余边界由真实长任务回归继续覆盖 |
| 浏览器能力对齐 | 动作与作用域已实现，真实配对验证未完成 | 导航、页面读取、点击/输入、等待、截图和失败收口使用真实浏览器状态；工具清单、权限、超时、重试和会话生命周期与命令行能力同样可观测；仍需可用供应商和原生 harness 做同场景验证 |
| S3.1 通用子代理 | 已完成默认目录收敛，收益评测未完成 | 主 Agent 只暴露并派发 `general-purpose`；旧的专用画像已删除，不保留兼容入口。仍需用 held-out 任务验证通用派发的收益、授权、工具角色、终端分享和副作用边界 |

## 每轮硬门禁

每个影响 Agent、工具、会话、浏览器、子代理或提示词的代码轮次都要执行下面四层检查，并把结果与最终提交 SHA 绑定：

1. **确定性回归**：运行受影响的 pytest、全量 Agent 测试、类型/格式/架构 ratchet 和必要的全量测试。任何已知基线失败都要在未改动基线复现后再归因。
2. **真实 MoviePilot 运行**：使用评测配置中显式声明的供应商模型和支持档位（本轮为 `gemini-2.5-pro` + `high`），在隔离 worker 中跑固定场景，保留真实模型请求、工具轨迹、耗时、token、终态和进程收尾证据。业务副作用只能进入评测假世界，不能把测试凭据或私有响应写入报告。
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

## 工具与 Harness 对齐清单

- **命令行**：普通一次性命令与后台终端分别支持 pipe/PTY、共享 cwd/shell/login/环境和 UTF-8；输入写入、空写入、EOF、分页、短写、interrupt、kill、超时、取消和进程组收尾都有结构化终态。
- **浏览器**：工具目录必须明确导航、读取、交互、等待和截图的动作与权限；浏览器会话、页面状态和失败重试由宿主持有，不能由模型字符串冒领。
- **子代理**：主 Agent 自动选择通用子代理；专用画像只有在 held-out 任务上证明提高成功率、减少调用或降低副作用风险时才保留。子代理不能自行发送消息、执行高影响写操作或继承兄弟任务句柄。
- **API Skill 与合同读取**：`skills/moviepilot-api/SKILL.md` 只负责路由索引和工作流；`api/*.md` 各自包含完整操作合同及该类别需要的 Body Models，不再依赖单独的 `api/models.md`。真实 Agent 先用 `read_skill` 得到 supporting-file 清单，再用同一个工具的 `file=api/<category>.md` 参数按需读取类别文档；评测目录必须提供同一条链路，不能只返回文件名而不给模型读取能力。operation 输入错误还要返回该 operation 的允许字段、必填字段和类型约束，帮助模型纠正后重试。
- **长任务与新消息**：入站消息在任务运行时仍可接受并进入有界队列；应用到下一模型边界时必须保留 tool-call/tool-result 配对和取消语义，不能只把文本拼到系统提示词。
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
- 浏览器、真实命令行/PTY 和 WebAgent 中途消息排队已有确定性实现，但仍缺少与原生 Codex 在同一模型、同一场景下的真实配对证据，不能把这些能力标为“已对齐”。
