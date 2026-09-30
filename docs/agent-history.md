# Agent 历史消息检索

本实现对照 NousResearch/hermes-agent 的固定提交
[`f42f579cf8bac4918ac9599bece71618afadd846`](https://github.com/NousResearch/hermes-agent/tree/f42f579cf8bac4918ac9599bece71618afadd846)。
对齐的是存储、召回、上下文展开及调用时机，不以同名工具或提示词代替实际行为。
检索不调用模型，不产生新的活动摘要；仍保留控制当前模型上下文窗口所需的压缩。

## 对照关系

| Hermes 实现 | MoviePilot 实现与行为 |
|---|---|
| `hermes_state.py`、`hermes_state_common.py`：profile 下独立 `state.db` | `agent/history/storage.py`、`schema.py`：Agent 运行目录、每用户独立 SQLite，主库可使用任意已支持后端 |
| `sessions` / `messages`、稳定消息身份 | 会话元数据与逐条 user/assistant/tool 原文、工具调用关联；唯一消息 ID 幂等追加 |
| 外部内容 FTS5、触发器原子同步 | 正文只保存一次，FTS 读取固定视图；正文与索引在同一事务提交 |
| 标准工具正文前缀 8192、工具 JSON 独立列 | 标准索引保持同一投影；显式 `role_filter="tool"` 搜原始完整工具正文 |
| trigram 排除 tool、cron、subagent 及工具调用 JSON | 相同索引范围，减少大结果和重复自动任务的索引体积 |
| `native/fts5_cjk` 的 `cjk_unicode61` | 原版 C 源码随仓库提供，附固定来源和 MIT 许可；可选安装，不用自行生成 n-gram 替代分词语义 |
| `hermes_state_search.py`：BM25、短语/布尔/前缀、子串及 OR 重试 | `agent/history/query.py` / `search.py`：普通词首先 FTS5；无结果时子串索引重试，最后允许非 CJK 多词查询 OR 放宽；显式 OR/NOT 不放宽 |
| CJK → bigram → trigram → LIKE | 两字中文在可选 tokenizer 可用时走 bigram，三字可走 trigram；单字、缺失索引和显式工具全文回退，响应报告实际路径 |
| `tools/session_search_tool.py`：300 候选、默认3/最多10会话、谱系去重 | 相同预算；隐藏 subagent/tool/kanban，cron 排在交互会话后；排除已看谱系；标题优先尝试最新同名续篇 |
| 当前上下文排除、压缩及 reset 前史可见 | 活动消息与压缩归档分别标记；压缩只有在主模型成功提交新状态后生效；原文始终保留 |
| adaptive、read、scroll、browse | 首命中 ±5 消息和首尾各3条，其余只给锚点；阅读首20/尾10；锚点窗口最多各20；近期浏览先按索引限量取候选 |
| 大结果展示限制 | 首尾1200、发现/窗口4000、阅读2000字符，保留截断标志与原始长度；不会把展示裁剪当成原文存储限制 |
| 分批 FTS 回填、高水位与进度 | 回填期间新写立即建索引，删除只对已索引行发送 FTS delete；游标与每批索引原子提交，可中断续跑；批次间暂停至少 0.2 秒或本批耗时的四倍，沿用 Hermes 的写入让行策略 |
| 派生索引故障不覆盖原始消息 | 确认的 FTS 虚表损坏先拆触发器并标记缺口；结构性数据库损坏明确报不可用，不擅自删除或重置用户库 |

## 路径、隔离和生命周期

文件位于 `config/agent/runtime/history/users/<user-key>/state.db`，`user-key` 与现有用户记忆一样来自不可逆稳定身份键。工具参数不能提供其他用户、profile、数据库或扩展库路径，管理员 Agent 也不隐式搜索其他用户的会话。目录权限为 0700，数据库文件为 0600；WAL、短连接和宿主有界 worker 分担读写，不在事件循环中扫描 SQLite。

主库的 `agentchat.agent_messages` 仍是恢复快照，独立消息库是压缩前的证据源。升级前尚存快照通过只读适配器分批搬迁，完成游标保存在独立库；完成后查询不访问主库。结果中的 `legacy_import.complete=false` 表示旧历史还没搬完；`legacy_snapshot` 消息不能用导入时间推断实际事件发生时间，更不能声称恢复了升级前已丢失的消息。

后台索引与快照搬迁由 `TaskRegistry` 持有，每用户最多一个、全服务最多八个任务。每批归还数据库 worker，进度落盘后才进入下一批。关停取消不会把半批当成成功，重启或再次使用时可继续。索引未就绪时结果公开能力状态，旧消息仍可经过受限回退查找。

用户定时任务按 `cron` 独立记录，仍可搜索但降低排名；没有真实用户身份的内部任务不进入个人历史。恢复快照自动过期不作为独立库的删除事件；独立库自己遵循 `DATA_CLEANUP_ENABLE` 与 `DATA_CLEANUP_AGENT_CHAT_DAYS`，后台维护按自身 `last_active` 分批清理，零天关闭自动清理。显式会话删除先删除独立证据并记录墓碑，再删除主库显示记录；主库失败时可重试，不能声称两个数据库具有同一个事务。

## 中文分词器

标准 FTS5 和 trigram 来自 SQLite。可选 CJK tokenizer 源码在 `native/fts5_cjk/`，与 Hermes 的可选本地扩展机制一致。按运行环境架构编译并安装到固定目录 `config/agent/runtime/lib/libfts5_cjk.so`，具体命令见该目录 README；Agent 请求不会调用编译器。每次连接只临时开启扩展加载，加载后立即关闭。

没有 CJK 扩展时，两字中文会走 LIKE；这与 Hermes 的能力回退一致，但不等于索引速度。SQLite VM 查询期限限制扫描，超时返回 `query_timeout`/未完成，不能解读为“没有相关内容”。安装扩展后的旧消息在后台分批建索引，期间继续报告索引未就绪。不要跨平台复制已编译二进制。

## 能力边界和验证

FTS5 是词项检索，不保证任意语义改写都能命中。模型应依据结果用同义词、对象编号、OR、时间区间和已看会话排除继续查询，再用锚点上下文核实。用户明确提供的 URL、文件或实时系统仍然优先读取，不能仅凭历史无命中判断目标不存在。完整存储也不等于完整展示，不能用被截断的单条证据断言工具成功。

`tests/test_agent_recall_persistence.py` 覆盖身份隔离、FTS 查询语义、CJK 原版分词器、索引回填并发写删、原文/展示分离、压缩可见性、真实 LangChain 工具回执、超时、删除与保留期；所有数据均为临时测试数据，无真实业务网络请求。查询计划验证 FTS 虚表和会话索引；规模与真实模型评估应单独报告数据量、索引能力、耗时和未验证范围，不能由单元测试通过推断智能提升。

阶段五补充了真实 LangChain 图回归：最终请求压缩完成后，以实际已提交状态的消息 ID 标记移出原文；模型调用失败时不提前提交压缩。成功的同一会话仍可从独立 FTS 证据库召回被压缩内容，下一会话读取的是原文证据而非压缩摘要。后台学习组合测试进一步验证十次真实工具回合触发复盘，记忆与技能落盘后由同一用户下一会话加载，另一用户目录保持隔离。
