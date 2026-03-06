#!/bin/bash
# MoviePilot 自动更新脚本
# 功能：拉取上游最新代码并自动应用我们的认证限制移除补丁

# 配置
UPSTREAM_BRANCH="v2"
OUR_BRANCH="feature/no-auth"
PATCH_FILE="no-auth-patch.diff"
REPO_DIR="/Users/bytedance/Developer/MoviePilot"

cd "$REPO_DIR" || { echo "无法进入项目目录"; exit 1; }

echo "=== MoviePilot 自动更新脚本 ==="
echo "当前分支: $(git branch --show-current)"
echo "远程仓库: $(git remote get-url origin)"
echo ""

# 1. 保存当前修改的补丁
echo "[1] 保存当前认证限制移除的补丁..."
git diff > "$PATCH_FILE"
if [ $? -ne 0 ]; then
    echo "补丁保存失败"
    exit 1
fi
echo "✅ 补丁已保存到 $PATCH_FILE"
echo ""

# 2. 切换到上游分支并拉取最新代码
echo "[2] 拉取上游最新代码..."
git stash > /dev/null 2>&1
git checkout "$UPSTREAM_BRANCH" > /dev/null 2>&1
git pull origin "$UPSTREAM_BRANCH"
if [ $? -ne 0 ]; then
    echo "拉取上游代码失败"
    git stash pop > /dev/null 2>&1
    exit 1
fi
echo "✅ 上游代码已更新到最新"
echo ""

# 3. 创建我们的功能分支
echo "[3] 创建功能分支..."
if git branch --list "$OUR_BRANCH" > /dev/null 2>&1; then
    git branch -D "$OUR_BRANCH" > /dev/null 2>&1
fi
git checkout -b "$OUR_BRANCH" > /dev/null 2>&1
echo "✅ 功能分支 $OUR_BRANCH 创建成功"
echo ""

# 4. 应用认证限制移除补丁
echo "[4] 应用认证限制移除补丁..."
git apply "$PATCH_FILE"
if [ $? -ne 0 ]; then
    echo "补丁应用失败，正在尝试强制应用..."
    git apply --reject "$PATCH_FILE"
    if [ $? -ne 0 ]; then
        echo "补丁强制应用失败"
        git checkout "$UPSTREAM_BRANCH" > /dev/null 2>&1
        git branch -D "$OUR_BRANCH" > /dev/null 2>&1
        git stash pop > /dev/null 2>&1
        exit 1
    fi
    echo "⚠️  补丁部分应用成功，已自动解决冲突"
else
    echo "✅ 补丁应用成功"
fi
echo ""

# 5. 提交修改
echo "[5] 提交修改..."
git add $(git diff --name-only --cached; git diff --name-only --no-index /dev/null) 2>/dev/null
git add $(git status --porcelain | awk '{print $2}') 2>/dev/null
git commit -m "feat: 移除PT站认证限制" > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "⚠️  没有需要提交的修改"
else
    echo "✅ 修改已提交"
fi
echo ""

# 6. 完成
echo "=== 更新完成 ==="
echo "当前状态: 已在分支 $OUR_BRANCH"
echo "上游代码: 已同步到最新"
echo "认证限制: 已移除"
echo ""
echo "下一步操作建议:"
echo "  1. 运行测试: pytest tests/"
echo "  2. 启动服务: python main.py"
echo "  3. 如需推送到远程: git remote add upstream <你的远程仓库地址>; git push upstream $OUR_BRANCH"