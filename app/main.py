import multiprocessing
import os
import signal
import sys
import threading
from types import FrameType

import uvicorn as uvicorn
from PIL import Image
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn import Config

from app.utils.system import SystemUtils

# 禁用输出
if SystemUtils.is_frozen():
    sys.stdout = open(os.devnull, 'w')
    sys.stderr = open(os.devnull, 'w')

from app.core.config import settings, global_vars
from app.core.module import ModuleManager
from app.core.plugin import PluginManager
from app.db.init import init_db, update_db, init_super_user
from app.helper.thread import ThreadHelper
from app.helper.display import DisplayHelper
from app.helper.resource import ResourceHelper
from app.helper.sites import SitesHelper
from app.helper.message import MessageHelper
from app.scheduler import Scheduler
from app.command import Command, CommandChian
from app.schemas import Notification, NotificationType

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
    from app.api.servcookie import cookie_router
    # API路由
    App.include_router(api_router, prefix=settings.API_V1_STR)
    # Radarr、Sonarr路由
    App.include_router(arr_router, prefix="/api/v3")
    # CookieCloud路由
    App.include_router(cookie_router, prefix="/cookiecloud")


def start_frontend():
    """
    启动前端服务
    """
    # 仅Windows可执行文件支持内嵌nginx
    if not SystemUtils.is_frozen() \
            or not SystemUtils.is_windows():
        return
    # 临时Nginx目录
    nginx_path = settings.ROOT_PATH / 'nginx'
    if not nginx_path.exists():
        return
    # 配置目录下的Nginx目录
    run_nginx_dir = settings.CONFIG_PATH.with_name('nginx')
    if not run_nginx_dir.exists():
        # 移动到配置目录
        SystemUtils.move(nginx_path, run_nginx_dir)
    # 启动Nginx
    import subprocess
    subprocess.Popen("start nginx.exe",
                     cwd=run_nginx_dir,
                     shell=True)


def stop_frontend():
    """
    停止前端服务
    """
    if not SystemUtils.is_frozen() \
            or not SystemUtils.is_windows():
        return
    import subprocess
    subprocess.Popen(f"taskkill /f /im nginx.exe", shell=True)


def start_tray():
    """
    启动托盘图标
    """

    if not SystemUtils.is_frozen():
        return

    if not SystemUtils.is_windows():
        return

    def open_web():
        """
        调用浏览器打开前端页面
        """
        import webbrowser
        webbrowser.open(f"http://localhost:{settings.NGINX_PORT}")

    def quit_app():
        """
        退出程序
        """
        TrayIcon.stop()
        Server.should_exit = True

    import pystray

    # 托盘图标
    TrayIcon = pystray.Icon(
        settings.PROJECT_NAME,
        icon=Image.open(settings.ROOT_PATH / 'app.ico'),
        menu=pystray.Menu(
            pystray.MenuItem(
                '打开',
                open_web,
            ),
            pystray.MenuItem(
                '退出',
                quit_app,
            )
        )
    )
    # 启动托盘图标
    threading.Thread(target=TrayIcon.run, daemon=True).start()


def check_auth():
    """
    检查认证状态
    """
    if SitesHelper().auth_level < 2:
        err_msg = "用户认证失败，站点相关功能将无法使用！"
        MessageHelper().put(f"注意：{err_msg}", title="用户认证", role="system")
        CommandChian().post_message(
            Notification(
                mtype=NotificationType.Manual,
                title="MoviePilot用户认证",
                text=err_msg,
                link=settings.MP_DOMAIN('#/site')
            )
        )


def singal_handle():
    """
    监听停止信号
    """

    def stop_event(signum: int, _: FrameType):
        """
        SIGTERM信号处理
        """
        print(f"接收到停止信号：{signum}，正在停止系统...")
        global_vars.stop_system()

    # 设置信号处理程序
    signal.signal(signal.SIGTERM, stop_event)
    signal.signal(signal.SIGINT, stop_event)


@App.on_event("shutdown")
def shutdown_server():
    """
    服务关闭
    """
    # 停止模块
    ModuleManager().stop()
    # 停止插件
    PluginManager().stop()
    PluginManager().stop_monitor()
    # 停止事件消费
    Command().stop()
    # 停止虚拟显示
    DisplayHelper().stop()
    # 停止定时服务
    Scheduler().stop()
    # 停止线程池
    ThreadHelper().shutdown()
    # 停止前端服务
    stop_frontend()


@App.on_event("startup")
def start_module():
    """
    启动模块
    """
    # 初始化超级管理员
    init_super_user()
    # 虚拟显示
    DisplayHelper()
    # 站点管理
    SitesHelper()
    # 资源包检测
    ResourceHelper()
    # 加载模块
    ModuleManager()
    # 安装在线插件
    PluginManager().install_online_plugin()
    # 加载插件
    PluginManager().start()
    # 启动定时服务
    Scheduler()
    # 启动事件消费
    Command()
    # 初始化路由
    init_routers()
    # 启动前端服务
    start_frontend()
    # 检查认证状态
    check_auth()
    # 监听停止信号
    singal_handle()


if __name__ == '__main__':
    # 启动托盘
    start_tray()
    # 初始化数据库
    init_db()
    # 更新数据库
    update_db()
    # 启动API服务
    Server.run()
