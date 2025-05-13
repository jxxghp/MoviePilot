import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.startup.workflow_initializer import init_workflow, stop_workflow
from app.startup.modules_initializer import shutdown_modules, start_modules
from app.startup.plugins_initializer import init_plugins_async, stop_plugins
from app.startup.routers_initializer import init_routers


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    定义应用的生命周期事件
    """
    print("Starting up...")
    # 启动模块
    start_modules(app)
    # 初始化工作流动作
    init_workflow(app)
    # 初始化路由
    init_routers(app)
    # 初始化插件
    plugin_init_task = asyncio.create_task(init_plugins_async())
    try:
        # 在此处 yield，表示应用已经启动，控制权交回 FastAPI 主事件循环
        yield
    finally:
        print("Shutting down...")
        try:
            # 取消插件初始化
            plugin_init_task.cancel()
            await plugin_init_task
        except asyncio.CancelledError:
            print("Plugin installation task cancelled.")
        except Exception as e:
            print(f"Error during plugin installation shutdown: {e}")
        # 清理模块
        shutdown_modules(app)
        # 关闭工作流
        stop_workflow(app)
        # 关闭插件
        stop_plugins()
