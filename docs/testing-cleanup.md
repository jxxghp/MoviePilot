# 后端测试清理审计

## 范围与方法

- 日期：2026-09-06。
- 基准：`v3`，`976cecce8f72291516be74419cfc23922e99eef0`，开始工作时本地与 `origin/v3` 一致。
- 对 `tests/test_*.py` 进行 AST 静态筛查，共 739 个文件、7,311 个测试函数/方法定义；该数字不是参数化展开后的 pytest 用例数。
- 筛查空执行体、同名覆盖、完全重复执行体、重复参数化输入、无直接断言和语句包含关系，再人工核对入口、fixture、输入、断言与状态差异。
- 未发现纯空测试或重复参数化输入。静态相似性只用于找候选，不是自动删除规则；本轮不声称所有测试均已完成逐条业务审计。

## 已清理

仅删除以下 6 个冗余测试定义，保留全部独立断言和被测入口。生产代码、公共测试设施、数据库迁移、兼容清单和质量门槛均不修改。

| 文件 | 删除用例 | 保留覆盖及依据 |
| --- | --- | --- |
| `tests/test_agent_background_output.py` | `test_background_non_streaming_does_not_send_by_default` | 同类的 `test_background_non_streaming_captures_without_sending_when_capture_only` 执行体完全相同。两者都显式设置 `ReplyMode.CAPTURE_ONLY`，旧名称实际没有验证默认值。 |
| `tests/test_agent_summarization_streaming.py` | `test_real_agent_does_not_recompact_small_tool_result_during_same_loop` | `test_real_agent_executes_compacted_tool_call_once` 使用相同真实 graph、模型响应及工具，已同时断言工具执行一次、摘要一次、模型调用两次和最终结果。合并说明以保留两种回归意图。 |
| `tests/test_fs_proxy_copy.py` | `test_worker_still_standalone_after_streaming_support` | `tests/test_fs_proxy.py::test_worker_does_not_import_app_package` 对同一 worker 源码做完全相同的断言；同文件还保留独立子进程启动验证。删除后顺带移除不再使用的 `Path` 导入。 |
| `tests/test_media_interaction.py` | `test_noai_prefix_preserves_traditional_interaction_priority_after_search` | `test_message_routes_text_reply_to_media_interaction_before_ai` 执行体完全相同。删除用例并未发送 `/noai`，只构造同一会话后发送 `1`；实际 `/noai` 创建会话的专项测试保留。 |
| `tests/test_plugin_database_lifecycle.py` | `test_remove_plugin_only_releases_the_database` | `test_stop_releases_the_database_and_never_destroys_it` 执行体完全相同，都只调用 `lifecycle.stop`。真正的卸载、删除数据及分身库销毁测试保留。 |
| `tests/test_metamusic.py` | `test_apply_title_keeps_single_word_title` | `test_apply_title_keeps_artist_abbreviation_dots` 使用相同输入 `E.S.Posthumus - Maraboot`，已有完全相同的曲名断言并额外断言艺术家。合并说明以保留单词曲名不应被当作发布组的回归意图。 |

## 未机械删除

- 架构、模块/事件合同、SDK/旧导入 ABI、数据库迁移测试：它们约束当前仍需支持的入口或升级路径，不是历史重构完成后即可删除的临时测试。
- `test_media_interaction.py` 的过期敏感输入测试：直接过期与被另一用户触发清理后的迟到输入属于不同状态，不能因后者包含前者的语句就删掉前者。
- `test_transfer_job_manager.py` 的相同元数据任务测试：是否调用 `migrate_task` 会改变作业关联状态，两条路径分别保留。
- `test_chain_external_ports.py` 的未装配与重复初始化/重置测试：即使最终都断言端口未配置，之前是否装配过的状态不同。
- `test_security_utils.py` 的公网 DNS 允许与异步缓存测试：保留独立同步安全决策与跨入口缓存契约，不以覆盖率重叠替代行为判断。
- PostgreSQL、平台和 Rust 条件跳过测试：条件代表受支持的运行环境，不能把本机未执行误判为失效。
- 无直接断言候选中，迁移验证、辅助断言、兼容导入和“不抛异常”测试仍有契约意义；不批量删除。
- `test_agent_image_support.py` 的显式关闭图片能力用例与布尔配置用例存在重叠，但属于存量 `TestCase` 文件。本轮不为删除一个分支用例触发整文件转换，后续修改该文件时按“改到即转”一起合并。

## 验证

所有命令均使用项目 `.venv` 对应的 `uv run --locked --no-sync` 环境。

| 验证 | 结果 |
| --- | --- |
| Agent 输出/压缩、文件代理、媒体交互、插件数据库生命周期，含保留 worker 用例的 6 个文件 | 清理前 160 passed，清理后 155 passed |
| `test_metamusic.py` | 清理前 86 passed，清理后 85 passed |
| 第一组生产代码覆盖 | 前后逐文件 `executed_lines`、`executed_branches` 集合完全一致，均覆盖 35,597 行、1,228 条分支 |
| 音乐标题组生产代码覆盖 | 前后逐文件集合完全一致，均覆盖 23,538 行、325 条分支 |
| 保留用例 AST 复核 | 排除 docstring 后，全部保留用例的函数定义一致；仅删除表中 6 个定义 |
| 清理后重复扫描 | 7,305 个测试定义，按本轮 AST 规则未再发现完全重复的执行体 |
| `python tests/run.py -q --durations=10` | 4 个分片全部通过，合计 8,437 passed、9 skipped |
| 改动 Python 文件 Pylint | 10.00/10，无诊断 |
| `git diff --check` | 通过 |

提交前已快进同步到 `be415175208c2f7a3c1ebf556cc80c72cf322e10`，上游音乐修复未触及本次清理文件。重新运行清理涉及的 7 个测试文件及上游音乐相关的 5 个测试文件，结果为 539 passed；上表的扫描数量和覆盖率仍对应审计基准，不混用不同提交的测量结果。

覆盖采集前后都有 `sysmon` 不支持动态 contexts 的提示，因此这里只比较汇总行/分支集合，不宣称逐用例 context 数据完整。全量运行结束阶段出现子进程/文件句柄的 `ResourceWarning`，不影响退出码；提示中的子进程在检查时已退出。本轮没有为消除警告修改公共测试设施。

覆盖率对比是辅助证据，删除决定仍以上述相同输入、状态和断言映射为准。本机为 macOS/Python 3.14，不写入 Ubuntu canonical 覆盖率基线。
