# PR-Agent 使用说明

本仓库通过 GitHub Actions 运行开源 PR-Agent，用于自动生成 PR 说明和进行 AI Review。

## Secrets

在仓库的 `Settings -> Secrets and variables -> Actions -> Repository secrets` 中配置：

- `OPENAI_KEY`：OpenAI 或 OpenAI 兼容服务的 API Key。
- `OPENAI_API_BASE`：OpenAI 兼容接口的 API Base，通常需要包含 `/v1`，以服务商文档为准。

`GITHUB_TOKEN` 使用 GitHub Actions 自动注入的 `${{ github.token }}`，不需要手工添加。

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

默认自动执行：

- `/review`：检查 PR 风险、潜在 bug、安全问题、测试缺口和可维护性问题。
- `/describe`：生成或更新 PR 描述、变更摘要和文件说明。

默认不自动执行：

- `/improve`：给出代码改进建议。这个工具更容易产生噪音和额外成本，建议先用评论命令手动触发。

## 常用评论命令

以下身份可在 PR 评论中使用：

- `OWNER`：仓库所有者。
- `MEMBER`：组织仓库中的组织成员。
- `COLLABORATOR`：仓库协作者。
- `CONTRIBUTOR`：曾经向仓库提交并合入过代码的贡献者。
- `FIRST_TIME_CONTRIBUTOR`：首次向仓库贡献 PR 的用户。

```text
/review
/describe
/improve
/ask 这次改动有没有遗漏权限校验？
```

评论触发依赖 `issue_comment` 事件。普通 issue 评论、Bot 评论、非允许身份评论、以及不以允许命令开头的评论都会跳过。

## 配置来源

PR-Agent 配置集中在 `.github/workflows/pr-agent.yml` 的 `env` 中维护。

当前主要设置：

- `config.model = "gpt-5.5"`：默认使用 GPT-5.5。
- `config.fallback_models = ["gpt-5.4"]`：主模型不可用时降级到 GPT-5.4。
- `config.reasoning_effort = "xhigh"`：使用更高审查推理强度。
- `config.ai_timeout = "900"`：模型调用最长等待 900 秒。
- `config.response_language = "zh-CN"`：让 PR-Agent 默认中文输出。
- `config.large_patch_policy = "clip"`：大 PR 截断分析，不直接跳过。
- `config.ignore_pr_title` / `config.ignore_pr_labels`：跳过自动生成 PR 或带 `skip pr-agent` 标签的 PR。
- `pr_reviewer.extra_instructions`：要求中文输出，优先指出 P0/P1 风险，并关注安全、权限、状态一致性、异步/缓存、副作用和测试缺口。
- `pr_reviewer.require_security_review = true`：要求输出安全审查部分。
- `pr_reviewer.require_tests_review = true`：要求输出测试审查部分。
- `pr_reviewer.enable_review_labels_effort = false`：不添加 `Review effort x/5` 工作量标签。
- `pr_reviewer.enable_review_labels_security = true`：保留明确安全风险标签。
- `pr_description.generate_ai_title = false`：默认不改 PR 标题。
- `pr_description.publish_labels = false`：默认不添加 PR 类型标签。
- `pr_description.enable_pr_diagram = false`：默认不生成图表。
- `pr_code_suggestions.focus_only_on_problems = true`：手动 `/improve` 时优先输出问题型建议。
- `pr_code_suggestions.suggestions_score_threshold = 7`：过滤低置信度建议。

标签来源：

- `/review` 可添加安全标签和工作量标签；当前只保留安全标签，关闭工作量标签。
- `/describe` 可按 PR 类型添加 `Bug fix`、`Tests`、`Bug fix with tests`、`Enhancement`、`Documentation`、`Other` 等标签；当前 `pr_description.publish_labels = false`，不会添加类型标签。
- 自定义标签默认未启用。

可按需再启用的工具配置：

- `[pr_update_changelog]`：配合 `/update_changelog` 生成 changelog 建议。
- `[pr_add_docs]`：配合 `/add_docs` 生成文档建议。
- `[pr_test]`：配合 `/test` 生成测试建议；它不会替代仓库自己的测试命令。
- `[pr_questions]`：配合 `/ask ...` 回答 PR 相关问题。

## 安全边界

PR-Agent Action 会读取 `OPENAI_KEY`，因此依赖的 Docker 镜像在 workflow 中固定版本号和 digest，
不使用浮动的 `latest` 或仅依赖可变 tag。

当前使用 `pull_request_target` 支持 fork PR 自动审查，但 workflow 不 checkout 或执行来自 fork 的代码，
只运行固定 digest 的 PR-Agent 容器并通过 GitHub API 读取 PR diff。`issue_comment` 属于 base repo
事件，因此评论命令只允许指定身份触发。

API Key 建议使用低额度、可轮换的专用 key。`OPENAI_API_BASE` 本身通常不是敏感信息，但继续按 secret 管理可以避免暴露服务商信息。

## 调整自动行为

自动行为在 workflow 的 `env` 中控制：

```yaml
config.model: "gpt-5.5"
config.fallback_models: '["gpt-5.4"]'
config.reasoning_effort: "xhigh"
config.ai_timeout: "900"
config.response_language: "zh-CN"
github_action_config.auto_review: "true"
github_action_config.auto_describe: "true"
github_action_config.auto_improve: "false"
github_action_config.pr_actions: '["opened", "reopened", "ready_for_review", "review_requested", "synchronize"]'
pr_description.generate_ai_title: "false"
pr_description.publish_labels: "false"
pr_description.enable_pr_diagram: "false"
pr_reviewer.enable_review_labels_effort: "false"
pr_reviewer.enable_review_labels_security: "true"
```

如果需要自动运行 `/improve`，把 `github_action_config.auto_improve` 改为 `"true"`。建议先观察手动 `/improve` 的质量和成本，再决定是否开启。
