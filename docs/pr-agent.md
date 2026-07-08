# PR-Agent 使用说明

本仓库通过 GitHub Actions 运行开源 PR-Agent，用于自动维护 PR 摘要、发布行内代码审查建议，并在每轮审查后发布一条简短的 Code Review 总结评论。

## 触发方式

`.github/workflows/pr-agent.yml` 监听：

- `pull_request_target`：PR 打开、重新打开、标记 ready、请求 review、推送新 commit 时自动运行。
- `issue_comment`：允许身份在 PR 评论里写允许的命令时手动运行。

PR 事件会自动执行受控审查，包含同仓 PR 和 fork PR。允许身份也可以在 PR 评论中使用允许的命令触发受控审查。
允许身份包括 `OWNER`、`MEMBER`、`COLLABORATOR`、`CONTRIBUTOR` 和 `FIRST_TIME_CONTRIBUTOR`。

## Workflow 权限

workflow 设置了最小可用权限：

- `contents: read`：读取仓库内容和 PR diff。
- `pull-requests: write`：更新 PR 描述、发布 PR Review 或修改 PR 相关元数据。
- `issues: write`：PR 评论在 GitHub API 中属于 issue comments，手动命令和总结评论需要该权限。

没有开启 `contents: write`。当前配置不让 PR-Agent 往仓库推代码或提交 changelog，因此不需要内容写权限。

## 自动行为

PR 事件默认自动执行：

- `/describe`：更新 PR Body 中的 `PR-Agent 摘要` / `PR-Agent Summary` 标记区域，保留用户原始描述。
- `/improve`：发布 GitHub 行内代码审查建议，不发布 PR-Agent 建议表格。

workflow 会在 `/improve` 后发布一条普通 PR 评论：

- 评论标题为 `## Code Review`。
- 如果本轮有新增行内建议，会基于这些建议生成自然语言总结。
- 如果本轮没有新增行内建议，直接发布无更多反馈的简短总结。
- 下一次运行前会删除上一条 PR-Agent Code Review 总结评论，避免评论堆叠，同时保留新的通知事件。

## 常用评论命令

以下身份可在 PR 评论中使用：

- `OWNER`：仓库所有者。
- `MEMBER`：组织仓库中的组织成员。
- `COLLABORATOR`：仓库协作者。
- `CONTRIBUTOR`：曾经向仓库提交并合入过代码的贡献者。
- `FIRST_TIME_CONTRIBUTOR`：首次向仓库贡献 PR 的用户。

```text
/describe
/improve
/ask 这次改动有没有遗漏权限校验？
```

评论触发依赖 `issue_comment` 事件。普通 issue 评论、Bot 评论、非允许身份评论、以及不以允许命令开头的评论都会跳过。

## 输出约定

PR-Agent 配置集中在 `.github/workflows/pr-agent.yml` 中维护。公开说明只描述用户可见行为：

- 根据 PR 标题和用户原始描述自动选择中文或英文；无法识别时默认中文。
- 保留用户原始 PR 描述，只更新 PR Body 中的 PR-Agent 标记区域。
- 不使用 PR-Agent 的 Reviewer Guide 输出。
- 不输出 PR Type、额外标签、图表或 describe 评论。
- 只发布值得维护者处理的问题型行内建议。
- 行内建议可使用 GitHub suggestion 形式，便于直接采纳。
- 没有建议时不发布 PR-Agent 建议表格，只保留简短的 Code Review 总结评论。

行内建议可使用风险前缀：

- `🔴 **High Risk**:`：高风险问题。
- `🟡 **Medium Risk**:`：中风险问题。
- `🔵 **Low Risk**:`：低风险问题。

可按需再启用的工具配置：

- `[pr_update_changelog]`：配合 `/update_changelog` 生成 changelog 建议。
- `[pr_add_docs]`：配合 `/add_docs` 生成文档建议。
- `[pr_test]`：配合 `/test` 生成测试建议；它不会替代仓库自己的测试命令。
- `[pr_questions]`：配合 `/ask ...` 回答 PR 相关问题。

## 安全边界

PR-Agent 依赖的 Docker 镜像在 workflow 中固定版本号和 digest，不使用浮动的 `latest` 或仅依赖可变 tag。

当前使用 `pull_request_target` 支持 PR 自动审查，但 workflow 不 checkout 或执行 PR 分支代码，只运行固定 digest 的 PR-Agent 容器并通过 GitHub API 读取 PR diff。`issue_comment` 属于 base repo 事件，因此评论命令只允许指定身份触发。
