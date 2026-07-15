# PR-Agent 使用说明

本仓库通过 PR Review Runner 运行 PR-Agent，帮助贡献者维护 PR 摘要、获取代码审查结果和提出 PR 相关问题。

## 自动执行

同仓分支和来自 fork 的 PR 都会自动执行 PR-Agent。

PR 在以下场景会自动处理：

- 打开或重新打开 PR。
- 将草稿 PR 标记为可审查。
- 请求审查。
- 每次推送新的 commit。

PR 带有 `skip pr-agent` 标签，或标题以 `[Auto]`、`Auto` 开头时，自动和手工路径都会跳过。

## 手工命令

在 PR 的普通讨论评论中使用以下命令：

```text
/describe
/review
/ask 这次改动有没有遗漏权限校验？
```

- `/describe`：更新 PR Body 内的 `PR-Agent 摘要`，并保留贡献者原有的 PR 描述。
- `/review`：发起一次代码审查。
- `/ask ...`：就当前 PR 提问，回复会发布在普通 PR 评论中。

本仓库禁用 `/improve` 及其等价别名。其他命令是否可用由 runner 所包含的 PR-Agent 能力决定。

手工命令仅允许以下 GitHub 身份关联的用户使用：`OWNER`、`MEMBER`、`COLLABORATOR`、`CONTRIBUTOR`、`FIRST_TIME_CONTRIBUTOR`。

新建的合法命令评论会触发执行；编辑后仍为合法命令的评论也会触发。编辑普通讨论评论不会调用模型。

## 审查结果

本仓库固定使用中文生成 PR-Agent 内容。`/describe` 的结果位于 PR Body 的 `PR-Agent 摘要` 区域，用于概览本次变更。

`/review` 和自动审查会通过原生 GitHub Review 发布，结果位于 Review 页签，标题固定为 `PR-Agent Code Review`。可定位到本次变更的问题会在对应代码行以行内评论呈现，并使用 high、medium 或 low 风险标识。

Review 摘要会自然概括本次变更和整体审查结论，不会复制行内评论。未发现需要处理的问题时，摘要会概括变更并自然说明暂无其他反馈。审查不会额外创建专用的 issue comment 摘要。

## 安全边界

自动审查通过 `pull_request_target` 在目标仓库上下文中读取 PR 信息，并使用共享 runner 的 `latest` 镜像，但不会 checkout 或执行 PR 分支代码。权限保持最小化：只授予读取仓库内容所需的 `contents: read`，以及更新 PR Body、发布 Review 和回复 PR 评论所需的写权限；不会向仓库推送代码或创建提交。
