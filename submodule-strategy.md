# 方案二：Git 子模块 + 分支策略（更高级的管理方案）

## 项目结构

```
MoviePilot/
├── .git/
├── .gitmodules          # 子模块配置
├── upstream/            # 上游代码（只读，直接引用官方仓库）
├── my-patches/          # 我们的修改补丁
│   └── no-auth-patch.diff
├── build/               # 构建产物
├── scripts/
│   ├── update.sh        # 自动更新脚本
│   └── apply-patches.sh # 应用补丁脚本
└── README.md
```

## 实现步骤

### 1. 创建子模块结构

```bash
cd /Users/bytedance/Developer/MoviePilot

# 初始化空仓库（如果尚未初始化）
git init
git remote add origin <你的远程仓库地址>

# 添加上游仓库作为子模块（upstream 目录）
git submodule add -b v2 https://github.com/jxxghp/MoviePilot.git upstream

# 创建我们的修改目录
mkdir -p my-patches
cp /path/to/no-auth-patch.diff my-patches/

# 创建构建目录
mkdir -p build

# 创建脚本目录
mkdir -p scripts
```

### 2. 构建脚本

**scripts/apply-patches.sh**：
```bash
#!/bin/bash

SRC_DIR="$(pwd)/upstream"
PATCH_DIR="$(pwd)/my-patches"
BUILD_DIR="$(pwd)/build"

echo "=== 应用补丁 ==="

# 清理并复制源文件
rm -rf "$BUILD_DIR"
cp -r "$SRC_DIR" "$BUILD_DIR"
cd "$BUILD_DIR" || exit 1

# 应用我们的补丁
echo "应用 no-auth-patch.diff..."
patch -p1 < "$PATCH_DIR/no-auth-patch.diff"
if [ $? -eq 0 ]; then
    echo "✅ 补丁应用成功"
else
    echo "❌ 补丁应用失败"
    exit 1
fi

# 构建项目（如果需要）
echo "构建项目..."
pip install -r requirements.txt
```

**scripts/update-upstream.sh**：
```bash
#!/bin/bash

cd /Users/bytedance/Developer/MoviePilot || exit 1

echo "=== 更新上游代码 ==="

# 更新子模块
git submodule update --init --recursive upstream
cd upstream || exit 1
git checkout v2
git pull origin v2

# 回到主项目
cd ..

# 重新应用补丁
./scripts/apply-patches.sh

# 提交更新
git add upstream my-patches build
git commit -m "Update from upstream: $(cd upstream && git rev-parse --short HEAD)"
git push origin main

echo "✅ 上游更新完成！"
```

### 3. 使用方法

**初始化**：
```bash
cd /Users/bytedance/Developer/MoviePilot
./scripts/apply-patches.sh
```

**启动项目**：
```bash
cd /Users/bytedance/Developer/MoviePilot/build
python main.py
```

**更新上游**：
```bash
cd /Users/bytedance/Developer/MoviePilot
./scripts/update-upstream.sh
```

## 优势与特点

### 优点

1. **完全隔离的修改**：我们的补丁完全独立于上游代码，不修改原始仓库
2. **版本清晰**：可以精确控制使用哪个上游版本和对应的补丁
3. **易于管理**：更新上游时只需要简单的命令
4. **可追溯性**：通过提交信息可以清楚地看到上游版本
5. **代码完整性**：可以在任何时候重新构建项目到当前状态

### 缺点

1. **每次更新都需要重新构建**
2. **需要维护一个单独的仓库**
3. **与标准的 Git 工作流程不同**

## 何时使用

- 需要严格控制修改内容
- 希望确保每次更新都是"干净"的
- 团队协作中，需要统一的更新流程
- 需要定期重新构建项目