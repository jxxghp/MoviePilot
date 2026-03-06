# Fork 仓库使用指南

## 基础操作

### 当前状态检查
```bash
cd /Users/bytedance/Developer/MoviePilot
git status
```

### 查看远程仓库
```bash
git remote -v
```

## 同步上游更新

### 手动同步（推荐）
```bash
cd /Users/bytedance/Developer/MoviePilot

# 1. 切换到 main 分支
git checkout main

# 2. 拉取上游最新代码
git fetch upstream

# 3. 合并上游更新到 main
git merge upstream/v2 --no-edit

# 4. 推送到你的仓库
git push origin main

# 5. 切换到功能分支
git checkout feature/no-auth

# 6. 合并 main 分支到功能分支
git merge main

# 7. 解决可能的冲突
# git status 查看冲突文件，手动修复后 git add <文件> && git commit

# 8. 推送到功能分支
git push origin feature/no-auth
```

### 使用同步脚本
```bash
cd /Users/bytedance/Developer/MoviePilot
./sync-upstream.sh
```

## 提交我们的修改

### 保存修改到补丁文件
```bash
cd /Users/bytedance/Developer/MoviePilot

# 1. 确保在我们的功能分支上
git checkout feature/no-auth

# 2. 查看已修改的文件
git status

# 3. 创建或更新补丁文件
git diff HEAD~1 HEAD > no-auth-patch.diff

# 4. 查看补丁内容（可选）
cat no-auth-patch.diff

# 5. 如果有新文件，需要添加到 git 中
git add <新文件路径>

# 6. 提交修改
git add <修改的文件>
git commit -m "feat: 更新认证限制移除逻辑"
git push origin feature/no-auth
```

## 推送修改到你的仓库

### 推送到 feature/no-auth 分支
```bash
cd /Users/bytedance/Developer/MoviePilot
git checkout feature/no-auth
git status  # 确保所有修改已提交
git push origin feature/no-auth
```

### 推送到 main 分支（不建议）
```bash
git checkout main
git merge feature/no-auth
git push origin main
```

## 解决常见问题

### 问题 1：无法推送到 origin
**原因**：通常是本地仓库与远程仓库不同步

**解决方案**：
```bash
# 1. 先从远程拉取更新
git pull origin main

# 2. 解决可能的冲突
git status
# 手动修改冲突文件
git add <冲突文件>
git commit -m "Merge remote-tracking branch 'origin/main'"

# 3. 再次尝试推送
git push origin main
```

### 问题 2：合并时产生冲突
**解决方案**：
```bash
# 查看哪些文件有冲突
git status

# 打开有冲突的文件，查找 <<<<<<< HEAD、=======、>>>>>>> 标记
# 手动修改文件，删除这些标记，保留你需要的内容
# 例如：
# Before:
# <<<<<<< HEAD
# def user_auth():
#     return True
# =======
# def user_auth():
#     print("authenticating")
#     return False
# >>>>>>> feature/no-auth

# After (保留我们的修改):
# def user_auth():
#     return True

# 4. 标记文件已解决
git add <冲突文件>

# 5. 完成合并
git commit -m "Resolve merge conflict"
```

### 问题 3：远程仓库拒绝合并
**解决方案**：
```bash
# 强制推送（谨慎使用，可能覆盖远程修改）
git push origin main --force
```

## 建议的工作流程

### 每日使用
1. 早上运行 `./sync-upstream.sh` 同步上游更新
2. 开发新功能或修复问题
3. 定期提交到 feature/no-auth 分支
4. 测试通过后，考虑合并到 main 分支

### 版本发布
1. 确保 main 分支是官方最新版本
2. 创建临时分支进行测试
3. 测试无误后合并到 main 分支
4. 创建 GitHub Release 进行备份

## 安全建议

1. **定期备份**：定期将重要分支推送到远程仓库
2. **分支管理**：不直接在 main 分支开发，使用功能分支
3. **代码审查**：对重要修改进行代码审查
4. **测试**：在合并到 main 分支前进行充分测试