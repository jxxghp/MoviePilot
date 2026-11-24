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

本轮为本地审阅交付，尚未提交、推送或发布；后续 CI 结果须按实际提交核对。真实企业微信测试跳过；其他真实平台和生产部署尚需对应环境验收。数据库回退要求恢复完整备份，详见[升级计划](plan.md)。

## 通用核心与平台实现分离

职责来源、最终 owner、完整调用链、兼容与架构边界以[平台边界文档](platform-boundaries.md)为准。

## 2026-11-24 平台职责分离验收

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
