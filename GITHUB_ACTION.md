# GitHub Action 自动化同步方案

## 概述

我们使用 GitHub Action 实现了自动同步上游代码并应用认证限制移除补丁的功能。这意味着你不需要每天手动运行 `sync-upstream.sh` 脚本，GitHub Action 会自动完成。

## 工作流文件

### 主要工作流：`auto-sync.yml`

**功能**：
- 每天早上 8 点（UTC+8）自动运行
- 同步上游代码到我们的 `feature/no-auth` 分支
- 自动应用认证限制移除补丁
- 处理可能的冲突

**文件位置**：
```
/Users/bytedance/Developer/MoviePilot/.github/workflows/auto-sync.yml
```

### 备用工作流：`sync-upstream.yml`（更复杂但更全面）

**功能**：
- 支持手动触发
- 支持 PR/Issue 评论触发
- 详细的冲突解决机制
- 状态报告

## 使用方法

### 1. 在你的仓库中启用 GitHub Action

**第一次设置**：
1. 在 GitHub 上访问你的仓库
2. 点击 "Actions" 标签
3. 你应该会看到我们的两个工作流
4. 点击 "Enable workflow" 按钮

### 2. 手动触发同步

**方法 1：通过 GitHub UI**：
1. 访问仓库的 "Actions" 页面
2. 在左侧选择工作流（如 Auto Sync Upstream）
3. 点击 "Run workflow" 按钮
4. 选择运行的分支（通常是 feature/no-auth）
5. 点击 "Run workflow"

**方法 2：通过 Issue 评论（如果配置了）**：
1. 创建一个新的 Issue
2. 在评论框中输入 `/sync`
3. 工作流会自动触发

### 3. 查看运行结果

1. 访问仓库的 "Actions" 页面
2. 点击最近的运行记录
3. 查看详细的日志输出

## 故障排除

### 问题 1：工作流未运行

**常见原因**：
- 工作流文件有语法错误
- 分支设置不正确
- GitHub Action 权限不足

**解决方案**：
1. 检查工作流文件语法
2. 确保 `ref: feature/no-auth` 分支存在
3. 检查仓库设置中的 Actions 权限

### 问题 2：无法推送到 main 分支

**常见原因**：
- 需要管理员权限才能强制推送到 main
- 分支受保护

**解决方案**：
```yaml
# 在工作流中添加权限
permissions:
  contents: write
  issues: write
  pull-requests: write
```

### 问题 3：补丁应用失败

**解决方案**：
1. 查看工作流日志中的错误信息
2. 检查 `no-auth-patch.diff` 文件的格式
3. 可能需要重新生成补丁

## 与本地开发的配合

### 本地开发时保持同步

**1. 从 feature/no-auth 分支开发**：
```bash
cd /Users/bytedance/Developer/MoviePilot
git pull origin feature/no-auth
```

**2. 如果需要修改我们的补丁**：
```bash
# 手动修改代码
./update.sh
git status
git commit -m "更新认证限制移除逻辑"
git push origin feature/no-auth
```

### 3. 验证同步是否成功

```bash
cd /Users/bytedance/Developer/MoviePilot
git log --oneline --stat | head -20
```

## 安全建议

### 1. 工作流权限设置
我们的工作流请求了以下权限：
```yaml
permissions:
  contents: write
  issues: write
  pull-requests: write
```
这些权限是必要的，但请确保你的仓库设置安全。

### 2. 分支保护
建议配置分支保护规则：
- 要求通过 CI 测试
- 要求至少 1 个代码审查
- 禁止直接推送到 main 分支

### 3. PR 合并策略
建议使用 Squash and merge，这样我们的修改历史更整洁。

## 长期维护建议

### 定期更新工作流

**检查上游仓库的分支**：
如果原始仓库的默认分支发生变化（如从 main 变为 master），需要相应地更新工作流。

**更新工作流文件**：
```bash
cd /Users/bytedance/Developer/MoviePilot
# 编辑 .github/workflows/auto-sync.yml
# 将 upstream/v2 更改为正确的分支
git commit -a -m "update upstream branch"
git push origin feature/no-auth
```

### 测试工作流

**定期手动运行工作流**：
建议每月手动触发一次工作流，确保它仍能正常工作。

## 总结

我们已经实现了一个可靠的自动化同步方案：
- ✅ 每天早上 8 点自动同步
- ✅ 自动应用我们的认证限制移除补丁
- ✅ 智能冲突解决
- ✅ 详细的运行日志
- ✅ 手动触发和问题评论触发

这个方案完全解决了每天早上手动运行脚本的问题，同时确保我们的修改始终保持在最新版本上。