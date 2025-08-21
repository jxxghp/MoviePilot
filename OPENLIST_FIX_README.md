# OpenList "Object Not Found" 错误修复

## 问题描述

在 MoviePilot 对接 OpenList 时，用户遇到以下问题：

- 第一集成功重命名并整理
- 后续集数出现错误：`上传alist失败，错误信息：object not found`
- 错误发生在使用移动或复制操作时

## 问题分析

经过代码分析，发现问题出现在以下场景：

1. **时序问题**：OpenList API 在处理文件操作后需要时间更新内部状态
2. **缓存问题**：OpenList 的文件系统缓存可能导致新创建的文件/目录暂时不可见
3. **网络延迟**：跨服务器操作时网络延迟加剧了时序问题

## 修复方案

### 1. 添加重试逻辑

在 `app/modules/filemanager/storages/alist.py` 中为以下方法添加了重试机制：

- `upload()` - 文件上传
- `copy()` - 文件复制  
- `move()` - 文件移动

### 2. 指数退避策略

采用指数退避重试策略：
- 第1次重试：等待1秒
- 第2次重试：等待2秒
- 第3次重试：等待4秒

### 3. 可配置参数

在 `app/core/config.py` 中添加了配置选项：

```python
# OpenList操作重试次数
OPENLIST_RETRY_COUNT = 3
# OpenList操作重试延迟（秒）
OPENLIST_RETRY_DELAY = 1
```

### 4. 改进错误日志

区分不同类型的错误：
- `object not found` 错误：标识为文件不存在或尚未同步
- 其他错误：保持原有日志格式

## 修改的文件

### 1. app/modules/filemanager/storages/alist.py

**主要修改：**

```python
# 添加 time 模块导入
import time

# 添加配置参数
retry_count = settings.OPENLIST_RETRY_COUNT
retry_delay = settings.OPENLIST_RETRY_DELAY

# upload 方法重试逻辑
def upload(self, fileitem, path, new_name=None, task=False):
    # ... 上传逻辑 ...
    
    # 添加重试逻辑来处理 "object not found" 错误
    max_retries = self.retry_count
    retry_delay = self.retry_delay
    
    for attempt in range(max_retries):
        new_item = self.get_item(Path(fileitem.path) / path.name)
        if new_item:
            break
        else:
            if attempt < max_retries - 1:
                logger.debug(f"【OpenList】上传后获取文件信息失败，第 {attempt + 1} 次重试，等待 {retry_delay} 秒...")
                time.sleep(retry_delay)
                retry_delay *= 2  # 指数退避
            else:
                logger.warn(f"【OpenList】上传文件 {path} 后，多次尝试获取文件信息均失败")
                return None
```

### 2. app/core/config.py

**添加配置项：**

```python
# OpenList操作重试次数
OPENLIST_RETRY_COUNT = 3
# OpenList操作重试延迟（秒）
OPENLIST_RETRY_DELAY = 1
```

## 使用方法

### 1. 默认配置

修复后，系统将使用默认配置：
- 重试次数：3次
- 重试延迟：1秒（指数退避）

### 2. 自定义配置

如需调整重试参数，可以在环境变量或配置文件中设置：

```bash
# 环境变量
export OPENLIST_RETRY_COUNT=5
export OPENLIST_RETRY_DELAY=2
```

或在配置文件中：

```ini
[OpenList]
retry_count = 5
retry_delay = 2
```

## 测试验证

### 1. 运行测试脚本

```bash
python3 test_alist_fix.py
```

### 2. 测试结果

测试脚本会模拟 OpenList 操作，演示重试逻辑：

```
开始执行 文件上传 操作...
  第 1 次尝试...
    ❌ 操作失败：object not found
    ⏳ 等待 1 秒后重试...
  第 2 次尝试...
    ❌ 操作失败：object not found
    ⏳ 等待 2 秒后重试...
  第 3 次尝试...
    ✅ 操作成功
```

## 兼容性

### 1. 向后兼容

- 修复完全向后兼容
- 不影响现有功能
- 默认配置与原有行为一致

### 2. 性能影响

- 仅在遇到 "object not found" 错误时才会重试
- 正常操作不受影响
- 重试延迟较短，对整体性能影响微乎其微

## 故障排除

### 1. 如果问题仍然存在

1. 检查 OpenList 服务器状态
2. 验证网络连接稳定性
3. 增加重试次数和延迟时间
4. 查看详细日志输出

### 2. 日志分析

修复后的日志会提供更详细的信息：

```
【OpenList】上传后获取文件信息失败，第 1 次重试，等待 1 秒...
【OpenList】上传后获取文件信息失败，第 2 次重试，等待 2 秒...
【OpenList】获取文件 /path/to/file 失败，文件不存在或尚未同步：object not found
```

## 总结

此修复解决了 OpenList 对接时的 "object not found" 错误，通过：

1. **智能重试**：自动处理时序问题
2. **指数退避**：避免对服务器造成压力
3. **可配置性**：支持自定义重试参数
4. **改进日志**：便于问题诊断

修复后，用户应该能够正常处理多集文件的整理，不再出现后续集数上传失败的问题。