# Agent 派生实现来源

本文集中记录派生实现的固定来源，代码注释只描述实际行为。

以下实现参考或移植自 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/tree/f42f579cf8bac4918ac9599bece71618afadd846)，固定提交为 `f42f579cf8bac4918ac9599bece71618afadd846`。上游版权为 Copyright (c) 2025 Nous Research，采用 MIT 许可；随代码保留完整许可和版权声明。

| 本地实现 | 上游来源 | 许可文件 |
|---|---|---|
| `app/agent/history/query.py` | `hermes_state_search.py` 的查询规范化和索引路由 | [MIT](../native/fts5_cjk/LICENSE.hermes) |
| `native/fts5_cjk/` | 同名目录的可选 CJK 分词器；具体作者及 SQLite 头文件说明见目录 README | [来源与许可](../native/fts5_cjk/README.md) |
| `app/agent/learning/matching.py` | `tools/fuzzy_match.py` 的九级模糊匹配 | [MIT](../app/agent/learning/LICENSE.hermes) |
| `app/agent/learning/threats.py` | `tools/threat_patterns.py` 的提示注入检测模式 | [MIT](../app/agent/learning/LICENSE.hermes) |
| `app/agent/learning/prompts.py` | `agent/background_review.py` 的记忆及技能复盘提示词；适配宿主工具名称与所有权操作 | [MIT](../app/agent/learning/LICENSE.hermes) |
| `app/agent/guardrails/controller.py`、`results.py` | `agent/tool_guardrails.py` 的循环检测与工具回执分类 | [MIT](../app/agent/guardrails/LICENSE.hermes) |
| `app/agent/guardrails/completion.py` | `agent/agent_runtime_helpers.py` 的回复结束纠偏；增加同规则的中文表达 | [MIT](../app/agent/guardrails/LICENSE.hermes) |
| `app/adapters/system/code/runner.py`、`client.py` | `tools/code_kernel.py` 的持久 cell runner 与 `tools/code_execution_tool.py` 的本地 RPC 客户端及辅助函数；替换环境变量前缀、按宿主 schema 生成签名、按字节保存 spill，父死亡及主动退出时收口进程组/树 | [MIT](../app/adapters/system/code/LICENSE.hermes) |

Python 会话生命周期与 RPC 设计另参考 `tools/code_kernel.py`、`tools/code_execution_rpc.py`、`tools/code_execution_env.py`：持久解释器、逐 cell 上下文、默认300秒/50次调用/4个空闲LRU/1800秒、后台回收及七天残留清理。宿主采用异步进程和 TaskRegistry，并注入既有有界线程池，未移植上游独立线程注册表；只读 helper 范围由 MoviePilot 的真实工具与 API 副作用合同决定。当前不实现远程 kernel，不把本地支持描述成远端后端对等。

存储与检索行为的对应关系见 [历史消息检索](agent-history.md)，后台复盘和执行纠偏的宿主边界见 [Agent 文档](agent.md)。来源一致不意味着未经验证的平台或运行场景已经通过验收。
