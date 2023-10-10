import multiprocessing
from pathlib import Path

import uvicorn as uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn import Config

from app.command import Command
from app.core.config import settings
from app.core.module import ModuleManager
from app.core.plugin import PluginManager
from app.db.init import init_db, update_db
from app.helper.display import DisplayHelper
from app.helper.sites import SitesHelper
from app.scheduler import Scheduler
from app.utils.system import SystemUtils

# App
App = FastAPI(title=settings.PROJECT_NAME,
              openapi_url=f"{settings.API_V1_STR}/openapi.json")

# 跨域
App.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_HOSTS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# uvicorn服务
Server = uvicorn.Server(Config(App, host=settings.HOST, port=settings.PORT,
                               reload=settings.DEV, workers=multiprocessing.cpu_count()))


def init_routers():
    """
    初始化路由
    """
    from app.api.apiv1 import api_router
    from app.api.servarr import arr_router
    # API路由
    App.include_router(api_router, prefix=settings.API_V1_STR)
    # Radarr、Sonarr路由
    App.include_router(arr_router, prefix="/api/v3")


def start_frontend():
    """
    启动前端服务
    """
    if not SystemUtils.is_frozen():
        return
    if SystemUtils.is_windows():
        nginx_path = settings.ROOT_PATH / 'nginx' / 'nginx.exe'
    else:
        nginx_path = settings.ROOT_PATH / 'nginx' / 'nginx'
    if Path(nginx_path).exists():
        import subprocess
        subprocess.Popen(f"start {nginx_path}", shell=True)


def stop_frontend():
    """
    停止前端服务
    """
    if not SystemUtils.is_frozen():
        return
    import subprocess
    if SystemUtils.is_windows():
        subprocess.Popen(f"taskkill /f /im nginx.exe", shell=True)
    else:
        subprocess.Popen(f"killall nginx", shell=True)


@App.on_event("shutdown")
def shutdown_server():
    """
    服务关闭
    """
    # 停止模块
    ModuleManager().stop()
    # 停止插件
    PluginManager().stop()
    # 停止事件消费
    Command().stop()
    # 停止虚拟显示
    DisplayHelper().stop()
    # 停止定时服务
    Scheduler().stop()
    # 停止前端服务
    stop_frontend()


@App.on_event("startup")
def start_module():
    """
    启动模块
    """
    # 虚拟显示
    DisplayHelper()
    # 站点管理
    SitesHelper()
    # 加载模块
    ModuleManager()
    # 加载插件
    PluginManager()
    # 启动定时服务
    Scheduler()
    # 启动事件消费
    Command()
    # 初始化路由
    init_routers()
    # 启动前端服务
    start_frontend()


if __name__ == '__main__':
    # 初始化数据库
    init_db()
    # 更新数据库
    update_db()
    # 启动服务
    Server.run()
