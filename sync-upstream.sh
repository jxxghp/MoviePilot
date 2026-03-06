# 方案三：Fork 仓库策略（推荐长期维护）

## 什么是 Fork 仓库

Fork 是 GitHub 提供的功能，允许你复制别人的仓库到自己的账户下，你可以在自己的仓库中自由修改，并随时从上游仓库同步更新。

## 步骤 1：在 GitHub 上创建 Fork

### 1.1 访问原始仓库
- 打开 GitHub，访问 https://github.com/jxxghp/MoviePilot
- 点击右上角的 "Fork" 按钮

### 1.2 配置 Fork 仓库
- 在弹出的对话框中，选择你的 GitHub 账户
- 等待 fork 完成

## 步骤 2：配置本地仓库

### 2.1 克隆你自己的仓库到本地
```bash
# 替换为你自己的仓库地址
git clone https://github.com/你的GitHub用户名/MoviePilot.git
cd MoviePilot
```

### 2.2 设置上游远程
这将允许你从原始仓库拉取更新：
```bash
git remote add upstream https://github.com/jxxghp/MoviePilot.git
git remote -v
```

### 2.3 验证远程配置
你应该看到类似这样的输出：
```
origin  https://github.com/你的用户名/MoviePilot.git (fetch)
origin  https://github.com/你的用户名/MoviePilot.git (push)
upstream        https://github.com/jxxghp/MoviePilot.git (fetch)
upstream        https://github.com/jxxghp/MoviePilot.git (push)
```

## 步骤 3：同步上游更新

### 3.1 保持本地 main 分支与上游同步
```bash
cd /Users/bytedance/Developer/MoviePilot

# 1. 切换到 main 分支
git checkout main

# 2. 拉取上游最新代码
git fetch upstream

# 3. 合并上游更新（无冲突时）
git merge upstream/v2 --no-edit

# 4. 推送到你的仓库
git push origin main
```

### 3.2 在功能分支上应用我们的修改
```bash
# 1. 创建并切换到功能分支
git checkout -b feature/no-auth

# 2. 应用认证限制移除补丁
# 如果使用 update.sh 脚本，它会自动应用
# 或者手动应用：git apply no-auth-patch.diff

# 3. 提交修改（可选，但建议）
git commit -a -m "feat: 移除PT站认证限制"

# 4. 推送到你的仓库
git push origin feature/no-auth
```

## 步骤 4：解决冲突

如果上游更新与我们的修改有冲突，Git 会停止合并并显示冲突文件。你需要：

### 4.1 查看冲突文件
```bash
git status
```

### 4.2 手动解决冲突
打开有冲突的文件，解决后：
```bash
git add <冲突文件>
git commit -m "Merge remote-tracking branch 'upstream/v2'"
git push origin feature/no-auth
```

## 步骤 5：定期同步的脚本

创建一个 `sync-upstream.sh` 脚本来自动化同步过程：

```bash
#!/bin/bash
# /Users/bytedance/Developer/MoviePilot/sync-upstream.sh

REPO_DIR="$(pwd)"

echo "=== 同步上游更新 ==="

# 1. 保持 main 分支与上游同步
echo "[1] 同步 main 分支..."
git checkout main > /dev/null 2>&1
if [ $? -ne 0 ]; then
    git branch main upstream/v2 > /dev/null 2>&1
    git checkout main > /dev/null 2>&1
fi
git fetch upstream > /dev/null 2>&1
git merge upstream/v2 --no-edit > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "❌ main 分支合并失败，存在冲突"
    git status
    exit 1
fi
git push origin main > /dev/null 2>&1
echo "✅ main 分支已同步"

# 2. 检查功能分支是否存在
if git branch --list "feature/no-auth" > /dev/null 2>&1; then
    echo "[2] 更新功能分支..."
    git checkout feature/no-auth > /dev/null 2>&1
    
    # 合并上游更新
    git merge main --no-edit > /dev/null 2>&1
    if [ $? -ne 0 ]; then
        echo "❌ 功能分支合并失败，存在冲突"
        git status
        exit 1
    fi
    
    git push origin feature/no-auth > /dev/null 2>&1
    echo "✅ 功能分支已更新"
else
    echo "[2] 创建功能分支..."
    git checkout -b feature/no-auth > /dev/null 2>&1
    git apply no-auth-patch.diff > /dev/null 2>&1
    git commit -a -m "feat: 移除PT站认证限制" > /dev/null 2>&1
    git push origin feature/no-auth > /dev/null 2>&1
    echo "✅ 功能分支已创建"
fi

echo "=== 同步完成 ==="
echo "当前状态: 在 feature/no-auth 分支"
echo "上游更新: 已同步到最新"
echo "认证限制: 已移除"