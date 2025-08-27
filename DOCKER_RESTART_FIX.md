# Docker Restart Fix for MoviePilot v2.7.7

## Issue Description

After updating to MoviePilot v2.7.7, users reported that the `/restart` command (or automatic/manual restart functionality) causes the container to exit with code 128, preventing the container from starting properly.

## Root Cause

The issue was introduced in commit `47c6917` which removed the graceful shutdown logic and replaced it with a direct Docker API restart. The problem occurs because:

1. **Incorrect Docker Client Configuration**: The system was configured to use `"tcp://127.0.0.1:38379"` as the Docker client API URL, but this endpoint is not accessible from within the container.

2. **Missing Error Handling**: The original code lacked proper error handling for Docker API operations.

3. **Container State Conflicts**: The restart was happening while the application was still running, causing conflicts.

4. **No Fallback Mechanism**: When Docker API is not available, the system had no fallback mechanism.

## Solution

The fix includes the following improvements:

### 1. Smart Docker Client Selection

```python
@staticmethod
def _get_docker_client():
    """
    获取Docker客户端，优先使用Unix socket
    """
    # 优先使用Unix socket
    if Path("/var/run/docker.sock").exists():
        return docker.DockerClient(base_url="unix://var/run/docker.sock")
    # 回退到配置的API地址
    return docker.DockerClient(base_url=settings.DOCKER_CLIENT_API)
```

### 2. Docker Connection Testing

```python
@staticmethod
def _test_docker_connection(client) -> bool:
    """
    测试Docker连接是否可用
    """
    try:
        client.ping()
        return True
    except Exception:
        return False
```

### 3. Enhanced Error Handling with Graceful Fallback

```python
try:
    # 创建 Docker 客户端
    client = SystemHelper._get_docker_client()
    
    # 测试Docker连接
    if not SystemHelper._test_docker_connection(client):
        # 如果Docker API不可用，尝试优雅退出
        logger.warning("Docker API不可用，尝试优雅退出...")
        return SystemHelper._graceful_exit()
    
    container_id = SystemHelper._get_container_id()
    if not container_id:
        return False, "获取容器ID失败！"
    
    # 获取容器对象
    container = client.containers.get(container_id)
    
    # 检查容器状态
    container.reload()
    if container.status != 'running':
        return False, f"容器状态异常：{container.status}"
    
    # 重启容器
    logger.info(f"正在重启容器 {container_id}...")
    container.restart()
    return True, ""
except docker.errors.NotFound:
    return False, "容器不存在或无法访问！"
except docker.errors.APIError as e:
    logger.warning(f"Docker API错误，尝试优雅退出: {str(e)}")
    return SystemHelper._graceful_exit()
except Exception as docker_err:
    logger.warning(f"重启时发生错误，尝试优雅退出: {str(docker_err)}")
    return SystemHelper._graceful_exit()
```

### 4. Graceful Exit Fallback

```python
@staticmethod
def _graceful_exit() -> Tuple[bool, str]:
    """
    优雅退出，依赖容器的重启策略
    """
    try:
        logger.info("执行优雅退出，依赖容器重启策略...")
        # 发送SIGTERM信号给当前进程
        os.kill(os.getpid(), signal.SIGTERM)
        return True, ""
    except Exception as e:
        return False, f"优雅退出失败: {str(e)}"
```

## How It Works

1. **Primary Method**: Try to use Docker API to restart the container
   - First attempt: Unix socket (`/var/run/docker.sock`)
   - Fallback: TCP endpoint (`tcp://127.0.0.1:38379`)

2. **Connection Testing**: Verify Docker API is accessible before attempting restart

3. **Graceful Fallback**: If Docker API is not available or fails:
   - Send SIGTERM signal to the current process
   - Rely on container restart policy (if configured)
   - This prevents the exit code 128 error

4. **Error Handling**: Comprehensive error handling for all failure scenarios

## Testing

To test the fix, you can run the provided test script:

```bash
python3 simple_docker_test.py
```

This script will:
- Check if the system is running in Docker
- Verify Docker socket availability
- Test Docker client creation
- Validate container access

## Compatibility

This fix maintains backward compatibility with:
- Existing Docker configurations
- Non-Docker environments
- Custom Docker client API configurations
- Containers with or without restart policies

## Files Modified

- `app/helper/system.py`: Main fix implementation
- `simple_docker_test.py`: Test script for validation

## Environment Variables

The system respects the `DOCKER_CLIENT_API` environment variable if set, but will automatically fall back to the Unix socket (`/var/run/docker.sock`) when available, which is the standard approach for Docker-in-Docker scenarios.

## Verification

After applying this fix:
1. The `/restart` command should work without causing exit code 128
2. Container restart should be graceful and successful
3. The application should start properly after restart
4. No manual intervention should be required
5. Works even when Docker API is not available (falls back to graceful exit)

## Container Restart Policy

For optimal results, ensure your container has a restart policy configured:

```yaml
# docker-compose.yml
services:
  moviepilot:
    restart: unless-stopped
    # ... other configuration
```

Or for Docker run:

```bash
docker run --restart unless-stopped moviepilot
```

## Related Issues

- GitHub Issue: #4856
- Commit: 47c6917129b7c2a1ffa4c6eaa095cfccc2355d49
- Affected Version: v2.7.7