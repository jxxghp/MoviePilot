import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.startup.workflow_initializer import init_workflow, stop_workflow
from app.startup.modules_initializer import init_modules, stop_modules
from app.startup.plugins_initializer import init_plugins, stop_plugins
from app.startup.routers_initializer import init_routers
from core.config import global_vars
from startup.command_initializer import init_command, stop_command
from startup.monitor_initializer import stop_monitor, init_monitor
from startup.scheduler_initializer import stop_scheduler, init_scheduler


async def init_extra_system():
    """
    初始化额外的系统（依赖于插件初始化完成）
    """
    await init_plugins()
    # 启动监控器
    init_monitor()
    # 启动定时器
    init_scheduler()
    # 启动命令
    init_command()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    定义应用的生命周期事件
    """
    print("Starting up...")
    # 初始化模块
    init_modules()
    # 初始化路由
    init_routers(app)
    # 初始化工作流
    init_workflow()
    # 初始化插件依赖系统
    extra_init_task = asyncio.create_task(init_extra_system())
    try:
        # 在此处 yield，表示应用已经启动，控制权交回 FastAPI 主事件循环
        yield
    finally:
        print("Shutting down...")
        # 停止信号
        global_vars.stop_system()
        try:
            extra_init_task.cancel()
            await extra_init_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(str(e))
        # 停止插件
        stop_plugins()
        # 停止命令
        stop_command()
        # 停止监控器
        stop_monitor()
        # 停止定时器
        stop_scheduler()
        # 停止工作流
        stop_workflow()
        # 停止模块
        stop_modules()
