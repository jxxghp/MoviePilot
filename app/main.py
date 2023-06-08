import os
import signal
import sys
from typing import Any

import uvicorn as uvicorn
from fastapi import FastAPI
from uvicorn import Config

from app.api.apiv1 import api_router
from app.core import settings, ModuleManager, PluginManager
from app.db.init import init_db, update_db
from app.helper.sites import SitesHelper
from app.scheduler import Scheduler

# App
App = FastAPI(title=settings.PROJECT_NAME,
              openapi_url=f"{settings.API_V1_STR}/openapi.json")

# API路由
App.include_router(api_router, prefix=settings.API_V1_STR)

# uvicorn服务
server = uvicorn.Server(Config(App, host=settings.HOST, port=settings.PORT, reload=False))


@App.on_event("shutdown")
def shutdown_server():
    """
    服务关闭
    """
    Scheduler().stop()


@App.on_event("startup")
def start_module():
    """
    启动模块
    """
    # 加载模块
    ModuleManager()
    # 加载插件
    PluginManager()
    # 加载站点
    SitesHelper()
    # 启动定时服务
    Scheduler()


def graceful_exit(signum: Any, frame: Any):
    """
    优雅退出
    """
    if server is not None:
        server.should_exit = True


# 注册退出信号处理函数
signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)

if __name__ == '__main__':
    # 初始化数据库
    init_db()
    # 更新数据库
    update_db()
    # 启动服务
    server.run()
