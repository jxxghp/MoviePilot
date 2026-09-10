# MoviePilot Agent 与 Codex Harness 对齐路线

更新时间：2026-09-11

这份文档是 Agent 能力对齐的交付路线和每轮验收合同。它记录当前证据与未完成目标，不把一次成功的模型调用当成“已经和 Codex 一样聪明”。最终判断必须同时看模型行为、工具执行、任务生命周期、业务终态和失败后的收敛结果。

## 当前目标

让 MoviePilot Agent 在 MoviePilot 的真实业务边界内具备可复核的 Codex 级 Harness 能力：模型能看到准确的工具合同，工具能完成完整的命令行、浏览器、终端输入输出、子代理和长任务协作闭环，宿主能隔离任务身份、收口进程并在新消息到达时继续推进。高影响业务动作仍由宿主的授权和确认策略控制。

真实模型评测的固定档位使用 **luna-max**：模型标识为 `gpt-5.6-luna`，推理预算为 `max`。报告必须同时记录这两个字段；更换模型、推理预算、提示集或判定器后，结果属于新的基线，不能与旧报告直接合并。

## 交付顺序

| 目标 | 状态 | 验收重点 |
| --- | --- | --- |
| C1.4 终端工具完整闭环 | 已完成基线 | `run/pipe/PTY` 共用 shell、cwd、login 和 UTF-8 策略；stdin 写入、EOF、分页、interrupt/kill、超时和进程组收尾有真实进程测试 |
| C1.3 终端任务作用域 | 本轮进行中 | 终端归属由宿主对象身份决定；定时运行、会话、子任务和内部工具管理器隔离；封口先于清理，排队或运行中的命令都不会在任务结束后迟到启动 |
| S2.3 运行中消息排队与 WebAgent 输入 | 下一目标 | 运行中仍可提交新消息；消息按会话原子入队，在下一次模型调用边界注入真实 `HumanMessage`；SSE 能报告 queued/applied，停止后不再派发后续工具 |
| 浏览器能力对齐 | S2.3 后续 | 导航、页面读取、点击/输入、等待、截图和失败收口使用真实浏览器状态；工具清单、权限、超时、重试和会话生命周期与命令行能力同样可观测 |
| S3.1 通用子代理 | S2.3 后 | 主 Agent 按任务动态派发通用子代理；保留专用画像必须有独立收益证据。子代理的授权、工具角色、终端分享和副作用边界不能因画像切换而放宽 |

## 每轮硬门禁

每个影响 Agent、工具、会话、浏览器、子代理或提示词的代码轮次都要执行下面四层检查，并把结果与最终提交 SHA 绑定：

1. **确定性回归**：运行受影响的 pytest、全量 Agent 测试、类型/格式/架构 ratchet 和必要的全量测试。任何已知基线失败都要在未改动基线复现后再归因。
2. **真实 MoviePilot 运行**：使用 `gpt-5.6-luna` + `max`，在隔离 worker 中跑固定场景，保留真实模型请求、工具轨迹、耗时、token、终态和进程收尾证据。业务副作用只能进入评测假世界，不能把测试凭据或私有响应写入报告。
3. **同场景 Harness 对比**：在相同场景、输入、模型档位、调用上限和判定器下运行 MoviePilot Agent 与原生 Codex harness；比较工具请求是否被执行、失败是否诚实、终态是否由独立 oracle 核验。原生 harness 不可用时报告 `blocked`，不能以离线脚本或单元测试替代。
4. **留存与复核**：报告目录使用 `evidence/agent-round/<commit-sha>/`（不提交凭据），至少包含 `model`, `reasoning_effort`, `scenario`, `agent_sha`, `harness_sha`, `model_calls`, `tokens`, `elapsed_seconds`, `tool_trace`, `business_oracle`, `process_oracle` 和失败分类。重复运行同一轮时保留每次报告，不能覆盖异常样本。

当前评测入口：

```bash
UV_PROJECT_ENVIRONMENT=/Users/jxxghp/MPProjects/MoviePilot/.venv \
uv run --locked --no-sync python -m scripts.evaluation \
  --live --scenario unknown_download \
  --model gpt-5.6-luna --reasoning-effort max \
  --output evidence/agent-round/<commit-sha>/moviepilot-unknown_download.json
```

同一场景的原生 harness 使用 `--native`，报告必须和 `--live` 放在同一个提交目录下。真实评测不能在 pytest 中隐式联网；命令失败、模型拒绝、token 不完整或 oracle 未核验都标记为未完成，不得记为通过。

## 工具与 Harness 对齐清单

- **命令行**：普通一次性命令与后台终端分别支持 pipe/PTY、共享 cwd/shell/login/环境和 UTF-8；输入写入、空写入、EOF、分页、短写、interrupt、kill、超时、取消和进程组收尾都有结构化终态。
- **浏览器**：工具目录必须明确导航、读取、交互、等待和截图的动作与权限；浏览器会话、页面状态和失败重试由宿主持有，不能由模型字符串冒领。
- **子代理**：主 Agent 自动选择通用子代理；专用画像只有在 held-out 任务上证明提高成功率、减少调用或降低副作用风险时才保留。子代理不能自行发送消息、执行高影响写操作或继承兄弟任务句柄。
- **长任务与新消息**：入站消息在任务运行时仍可接受并进入有界队列；应用到下一模型边界时必须保留 tool-call/tool-result 配对和取消语义，不能只把文本拼到系统提示词。
- **观察证据**：最终答案只能引用宿主记录的工具结果和业务 oracle；“模型说完成”不能替代数据库、下载器、浏览器或进程状态核验。

## 通过标准与未完成边界

“对齐”至少需要多个固定场景、多个重复轮次和一组未见场景在同一模型档位下持续通过，并且命令行、浏览器、消息排队、子代理和取消路径都具备失败证据。一次 live pilot、单个场景或单元测试通过只能证明局部合同成立，不能证明与 Codex 的整体智能相等。

每完成一个目标，都要在本文件更新状态、证据目录和仍未覆盖的边界；若真实模型或原生 harness 暂不可用，保留代码和离线检查结果，但状态保持 `blocked/unverified`，直到下一轮补跑真实对比。
