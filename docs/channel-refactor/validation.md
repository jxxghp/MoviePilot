# 消息渠道验证

导航：[设计](design.md) · [计划](plan.md) · [任务](tasks.md)

## 验证合同

后端用例通过临时 `CONFIG_DIR`、临时数据库和模拟外部协议隔离真实账号及服务。单元测试覆盖成功、拒绝、撤权、并发版本冲突、重放及失败恢复；不使用真实渠道批量发送作为验收要求。

| 场景 | 主要回归文件 |
| --- | --- |
| 迁移与配置导入 | `tests/test_channel_resources_migration.py`、`tests/test_channel_actor_migration.py` |
| 准入与操作范围 | `tests/test_channel_admission.py`、`tests/test_channel_authorization.py` |
| 会话与个人隔离 | `tests/test_channel_actor_boundaries.py`、`tests/test_interaction_scope.py` |
| 资源与版本、完整删除 | `tests/test_channel_settings.py`、`tests/test_notification_channel_manage.py` |
| 通知受众、时间及分段 | `tests/test_notification_rules.py`、`tests/test_notification_routing_parity.py`、`tests/test_outbox.py` |
| 不可变业务归属 | `tests/test_subscription_ownership.py`、`tests/test_download_task_service.py` |
| 待确认下载归属及防重 | `tests/test_download_pending_confirmation.py` |
| 已认证入站提交与共享认领失败 | `tests/test_message_ingress.py` |
| 渠道身份核验与缓存对账 | `tests/test_channel_runtime_initializers.py` |
| 设备登记与迟到清理 | `tests/test_webpush_ownership.py`、`tests/test_webpush_subscription.py` |

## 提交检查

执行锁定环境的完整 `uv run --locked --no-sync python tests/run.py`，改动文件及全应用 pylint、mypy、架构依赖、复杂度、并发、异步阻塞、任务归属、service locator、Ruff/mypy ratchet 和三轮启动性能检查。复杂度只允许固化下降，不提升预算；冷导入预算保持 3000ms。

第一轮验收已形成上述本地提交，未推送或发布；后续 CI 结果须按实际提交核对。真实企业微信测试跳过；其他真实平台和生产部署尚需对应环境验收。数据库回退要求恢复完整备份，详见[升级计划](plan.md)。

## 通用核心与平台实现分离

职责来源、最终 owner、完整调用链、兼容与架构边界以[平台边界文档](platform-boundaries.md)为准。

## 第一轮平台职责分离验收（4e666434e / af33d57d）

| 检查 | 最终结果 |
| --- | --- |
| 完整后端用例 | 锁定环境按 `tests/run.py --shard N/8` 覆盖全部测试文件，22,889 通过、22 跳过，8 个分片均成功 |
| 覆盖率 | 同一最终应用源码的全部分片原始数据合并，`coverage_ratchet.py --report ...` 通过；全 app 排除 plugins 为 82.50%，1,229 文件；新平台文件满足 80% 门槛 |
| 身份/平台专项 | 最小测试渠道、已认证别名、跨连接/群组、历史冲突、固定升级等价、凭据保护及 Slack 分段恢复通过 |
| 架构与 API | 全量中依赖、API/OpenAPI、包根及 SDK/Compat 治理通过；文档/快照/平台约束另复验 72 项通过 |
| pylint / mypy | PR base 到最终树的全部改动 Python 文件及全 app pylint 10.00/10；全宿主 mypy 无错误 |
| 静态 ratchet | 类型例外、Ruff/mypy、复杂度、并发、async、任务归属及 service locator 全部通过 |
| 启动性能 | 三轮中位数：factory 1855.832ms、main 1818.798ms、lifecycle 1832.358ms；原 3000ms 预算和冷导入模块数均保持 |
| 前端配套 | 全量 5,083 通过、1 跳过；format、lint、typecheck、build 通过 |
| 真实浏览器 | Chrome 桌面 1440×1000、移动 390×844，真实规则组件按能力显示非数字线程、编辑和保存；无控制台错误，无横向溢出。使用合成能力/数据，不代表真实租户端到端验收 |
| 真实企业微信 | 按用户要求继续跳过；企业微信凭据策略仅执行模拟外部 API 的离线测试 |

架构依赖与运行合同快照仅记录已审阅的 owner/调用变化；新增 1 个 `MessageAttachmentRead` schema 导出，没有删除或改名公开导出。原 13 种下载模块 ABI 保留，调用由平台读取计划描述。复杂度基线只移除三项旧豁免：附件图像 18→9、文件 19→5、音频 19→8；不提高复杂度、覆盖率或启动门槛，覆盖率基线未改。

源码与测试均位于独立工作树，原 PR 源码、当前手测源指针和实例未切换。精确暂存树、diff、日志与浏览器截图随本地 `review/manifest.json` 留档，后续提交可按“协议/用例实现”“回归与架构约束”“文档”划分职责；本轮不自动创建提交或操作远端。

## 独立平台边界补齐

以[独立平台验收与身份职责](platform-boundaries-completion.md)为本轮补齐设计和调用链；旧 Web 复用测试不再作为扩展完成的证据。

本轮新增验收以独立 `review-completion/manifest.json` 的最终源码树和日志为准；第一轮测试数与截图只代表其对应提交，不能冒充当前源码验收。


## 本轮补齐验收结果

| 检查 | 结果与证据范围 |
| --- | --- |
| 后端全量 | `CONFIG_DIR=<临时目录> uv run --locked --no-sync python -m coverage run --parallel-mode tests/run.py --shard N/8`，8 个分片全部成功：22,915 通过、22 跳过 |
| 最终新增 Schema 回归 | 同一应用源码另执行 `coverage run --parallel-mode -m pytest -q tests/test_channel_platform_extensions.py tests/test_channel_platform_architecture.py`：71 通过；其中公开 OpenAPI/JSON 往返是全量后新增的 1 项测试，不将重复执行计入全量总数 |
| 覆盖率 | 保留全量和补充测试原始数据，以 Coverage API `combine(..., keep=True)` 合并，`coverage_ratchet.py --report <coverage.json>` 通过；全 app 排除 plugins：82.51%，1,229 文件；canonical 覆盖率基线未修改 |
| 独立平台链路 | 12 个内置平台共存 + 独立传输；实际认证、宿主分派、申请、管理员 HTTP 审批、规则执行和拒绝；共享传输不同 provider 仍隔离 |
| 架构约束 | `channel_platforms.py`、`baseline.py --check-host` 和全量依赖测试通过；局部变量、枚举/导入别名和字典查表反例通过 |
| pylint / 类型 | PR base、HEAD 和本轮暂存变更的 Python 文件并集及全 app 均 10.00/10；新增 Schema 测试另复验 10.00/10；`mypy_policy.py`、`mypy_ratchet.py` 通过，0 类型错误 |
| 其他门禁 | `complexity.py`、`concurrency.py`、`async_blocking.py`、`task_ownership.py`、`service_locator.py`、`ruff_ratchet.py` 通过；原门槛保持 |
| 启动性能 | `performance.py --check --repeat 3` 通过；中位数 factory 1881.687ms、main 1825.886ms、lifecycle 1829.128ms；模块数与 3000ms 预算保持 |
| 前端本轮 | `yarn test:run`：5,084 通过、1 跳过；`yarn format:check`、`yarn lint`、`yarn typecheck` 通过；新增目录测试同时展示全部 12 个内置平台和独立平台，组件专项 12 项通过 |
| 前端构建/浏览器 | 本轮未改生产前端源码，复用 af33d57d 的构建及桌面/移动浏览器证据；本轮新增后端不宣称已做新的真实浏览器或租户验收 |
| 环境与交付 | 使用隔离测试配置，真实企业微信继续跳过；未推送、强推、合并、发布或切换手测实例。按用户要求将已验收改动提交本地，提交阶段不额外运行测试；最新 commit 和 tree 由审阅索引记录 |

完整日志、原始覆盖率数据位置与源码树哈希由本地 `review-completion/manifest.json` 索引；该 manifest 和 `current-source.json` 均指向实际前后端工作树。旧 PR 内容尚未同步，不能用远端链接代替本轮源码证据。最终应用源码与全量测试时一致；之后只增加 Schema 测试和更新文档，补充测试、覆盖率和静态检查已重跑。


## 扩展传输标识缓存修复

本次基于后端 `a2e36930a` 修复 `NotificationChannel._missing_`：外部合法标识只生成无缓存的枚举值对象，解析不写 `_value2member_map_` 或成员目录，不产生平台注册和授权。同类枚举保持按值相等及原名称哈希，内置成员仍是原单例；字符串不获得枚举身份。平台协议、连接 provider/namespace 和 Web/WebAgent 保留限制保持原设计。

- 新增批量回归先在旧实现复现：4,096 个合法未注册标识导致全局缓存增加 4,096 项；修复后直接解析及 Pydantic JSON 解析均不改变缓存、平台目录或授权记录。
- 404 项专项通过，覆盖同值比较、集合/字典、复制和 pickle/JSON 往返，及实际认证入站、申请、HTTP 审批、规则执行、连接隔离和保留身份限制。
- 改动文件及全应用 pylint 均 10.00/10，mypy 0 错误；Ruff、复杂度和渠道平台架构门禁通过，未改门槛或基线。
- 后端全量以隔离 `CONFIG_DIR` 执行 `uv run --locked --no-sync python tests/run.py --shard N/8`，8 个分片均成功：22,918 通过、22 跳过。前端生产及测试源码未变，本次未重跑前端；真实企业微信继续跳过。

本次精确源码与检查日志由本地 `review-enum-cache/manifest.json` 索引，`current-source.json` 和 `platform-boundaries-current.json` 的最新入口同时更新。此前全量覆盖率报告仍属于其原源码树，本次不将旧覆盖率数字标作新源码结果。运行实例与远端 PR 未随源码索引自动更新。
