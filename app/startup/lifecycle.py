import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.chain.system import SystemChain
from app.startup.command_initializer import init_command, stop_command, restart_command
from app.startup.memory_initializer import init_memory_manager, stop_memory_manager
from app.startup.modules_initializer import init_modules, stop_modules
from app.startup.monitor_initializer import stop_monitor, init_monitor
from app.startup.plugins_initializer import init_plugins, stop_plugins, sync_plugins, backup_plugins, restore_plugins
from app.startup.routers_initializer import init_routers
from app.startup.scheduler_initializer import stop_scheduler, init_scheduler, init_plugin_scheduler
from app.startup.workflow_initializer import init_workflow, stop_workflow
from app.helper.system import SystemHelper


async def init_extra():
    """
    同步插件及重启相关依赖服务
    """
    if await sync_plugins():
        # 重新注册插件定时服务
        init_plugin_scheduler()
        # 重新注册命令
        restart_command()
    # 设置系统已修改标志
    SystemHelper().set_system_modified()
    # 重启完成
    SystemChain().restart_finish()


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
    # 恢复插件备份
    restore_plugins()
    # 初始化插件
    init_plugins()
    # 初始化定时器
    init_scheduler()
    # 初始化监控器
    init_monitor()
    # 初始化命令
    init_command()
    # 初始化工作流
    init_workflow()
    # 初始化内存管理
    init_memory_manager()
    # 插件同步到本地
    sync_plugins_task = asyncio.create_task(init_extra())
    try:
        # 在此处 yield，表示应用已经启动，控制权交回 FastAPI 主事件循环
        yield
    finally:
        print("Shutting down...")
        # 取消同步插件任务
        try:
            sync_plugins_task.cancel()
            await sync_plugins_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(str(e))
        # 备份插件
        backup_plugins()
        # 停止内存管理器
        stop_memory_manager()
        # 停止工作流
        stop_workflow()
        # 停止命令
        stop_command()
        # 停止监控器
        stop_monitor()
        # 停止定时器
        stop_scheduler()
        # 停止插件
        stop_plugins()
        # 停止模块
        stop_modules()
