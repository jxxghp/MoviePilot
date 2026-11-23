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

本次提交的精确结果由关联 PR 描述及其 CI 记录维护。真实企业微信测试跳过；其他真实平台和生产部署尚需对应环境验收。数据库回退要求恢复完整备份，详见[升级计划](plan.md)。
