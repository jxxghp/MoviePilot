# 异步数据库操作指南

## 概述

本指南介绍如何在MoviePilot项目中实现异步数据库操作，而无需重写现有的同步数据库模块。

## 方案优势

1. **最小化改动**: 不需要重写现有的同步数据库操作代码
2. **渐进式迁移**: 可以逐步将需要异步的操作迁移到异步版本
3. **向后兼容**: 现有的同步代码继续正常工作
4. **性能提升**: 通过线程池执行同步数据库操作，避免阻塞事件循环

## 核心组件

### 1. 异步适配器 (`app/db/async_adapter.py`)

提供以下功能：
- `async_db_operation`: 将同步函数包装为异步函数
- `async_db_session`: 为异步操作提供数据库会话
- `AsyncDbOper`: 异步数据库操作基类
- `to_async_db_oper`: 将同步操作类转换为异步版本
- `AsyncDbSession`: 异步数据库会话上下文管理器

### 2. 异步操作类 (`app/db/async_user_oper.py`)

展示如何创建异步版本的数据库操作类。

## 使用方法

### 方法1: 使用装饰器自动转换

```python
from app.db.user_oper import UserOper
from app.db.async_adapter import to_async_db_oper

# 自动将同步类转换为异步版本
AsyncUserOper = to_async_db_oper(UserOper)

# 使用异步版本
async def example():
    async_user_oper = AsyncUserOper()
    users = await async_user_oper.list()
    return users
```

### 方法2: 手动创建异步方法

```python
from app.db.async_adapter import async_db_operation

class AsyncUserOperManual:
    def __init__(self, db: Session = None):
        self._sync_oper = UserOper(db)
    
    @async_db_operation
    def list(self) -> List[User]:
        return self._sync_oper.list()
    
    @async_db_operation
    def add(self, **kwargs):
        return self._sync_oper.add(**kwargs)
```

### 方法3: 使用异步上下文管理器

```python
from app.db.async_adapter import AsyncDbSession

async def example():
    async with AsyncDbSession() as db:
        # 直接使用同步操作，但在线程池中执行
        from functools import partial
        users = await asyncio.get_event_loop().run_in_executor(
            None, partial(lambda db: db.query(User).all(), db)
        )
    return users
```

### 方法4: 装饰器包装单个函数

```python
from app.db.async_adapter import async_db_operation

@async_db_operation
def get_user_by_name_sync(db: Session, name: str) -> User:
    return db.query(User).filter(User.name == name).first()

async def example():
    async with AsyncDbSession() as db:
        user = await get_user_by_name_sync(db, "admin")
    return user
```

## 在FastAPI中使用

### 依赖注入

```python
async def get_current_user_async_dependency(
    token_data: schemas.TokenPayload = Depends(verify_token)
) -> User:
    async with AsyncDbSession() as db:
        from functools import partial
        user = await asyncio.get_event_loop().run_in_executor(
            None, partial(User.get, db, rid=token_data.sub)
        )
        if not user:
            raise HTTPException(status_code=403, detail="用户不存在")
        return user

@router.get("/me/async")
async def get_current_user_async(
    current_user: User = Depends(get_current_user_async_dependency)
):
    return current_user
```

### API端点

```python
@router.get("/users/async")
async def get_users_async():
    async_user_oper = AsyncUserOper()
    users = await async_user_oper.list()
    return users
```

## 并发操作

### 批量操作

```python
@router.post("/users/batch-async")
async def create_users_batch_async(users_data: List[schemas.UserCreate]):
    async_user_oper = AsyncUserOper()
    
    # 并发创建用户
    tasks = []
    for user_data in users_data:
        task = async_user_oper.add(**user_data.dict())
        tasks.append(task)
    
    # 等待所有任务完成
    await asyncio.gather(*tasks)
    
    # 获取创建的用户列表
    all_users = await async_user_oper.list()
    return all_users[-len(users_data):]
```

### 复杂查询

```python
@router.get("/users/stats/async")
async def get_user_stats_async():
    async_user_oper = AsyncUserOper()
    
    # 并发执行多个数据库查询
    users_task = async_user_oper.list()
    active_users_task = async_user_oper.get_active_users()
    
    # 等待所有查询完成
    users, active_users = await asyncio.gather(users_task, active_users_task)
    
    return {
        "total_users": len(users),
        "active_users": len(active_users),
        "inactive_users": len(users) - len(active_users)
    }
```