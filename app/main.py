import os
import sys


def _prepare_direct_execution_import_path() -> None:
    """
    修正直接执行 ``app/main.py`` 时的模块搜索路径。

    PyCharm 的脚本启动方式会把 ``app`` 目录放到 ``sys.path[0]``，使应用内部
    目录可能被当成顶级模块并遮蔽标准库或第三方包。直接执行时只保留项目根目录
    作为应用导入入口，模块方式启动则不做任何调整。
    """
    if __package__:
        return
    script_dir = os.path.dirname(os.path.realpath(__file__))
    project_root = os.path.dirname(script_dir)
    sys.path[:] = [
        entry
        for entry in sys.path
        if os.path.realpath(entry or os.curdir) != script_dir
    ]
    if project_root in sys.path:
        sys.path.remove(project_root)
    sys.path.insert(0, project_root)


_prepare_direct_execution_import_path()

import setproctitle
import signal
import threading
from pathlib import Path
from typing import Optional

import uvicorn as uvicorn
from PIL import Image
from uvicorn import Config

from app.adapters.system.stdio import configure_rotating_stdio
from app.adapters.system.host import SystemUtils

stdio_log_file = os.getenv("MOVIEPILOT_STDIO_LOG_FILE")
if stdio_log_file:
    # 本地 CLI 会把 stdout/stderr 切到滚动日志，避免无限追加单独的大文件。
    configure_rotating_stdio(
        log_file=Path(stdio_log_file),
        max_bytes=max(int(os.getenv("MOVIEPILOT_STDIO_LOG_MAX_BYTES", "0") or 0), 1),
        backup_count=max(
            int(os.getenv("MOVIEPILOT_STDIO_LOG_BACKUP_COUNT", "0") or 0),
            0,
        ),
    )
elif SystemUtils.is_frozen():
    sys.stdout = open(os.devnull, 'w')
    sys.stderr = open(os.devnull, 'w')

from app.factory import app
from app.runtime.config import global_vars, settings
from app.runtime.topology import (
    UnsupportedProcessTopologyError,
    validate_process_topology,
)

setproctitle.setproctitle(settings.PROJECT_NAME)


class MoviePilotServer(uvicorn.Server):
    """在 Uvicorn 开始优雅退出前发布应用协作停止标志"""

    def handle_exit(self, sig, frame) -> None:
        global_vars.stop_system()
        super().handle_exit(sig, frame)


APP_FACTORY = "app.factory:create_app"
Server: Optional[MoviePilotServer] = None


def create_server() -> MoviePilotServer:
    """创建不带 reload/multiprocess supervisor 的单进程生产服务器。"""
    server = MoviePilotServer(
        Config(
            app,
            host=settings.HOST,
            port=settings.PORT,
            reload=False,
            workers=1,
            timeout_graceful_shutdown=60,
        )
    )
    # 数据库准备阶段收到的信号早于 Server 物化，创建后必须继承既有停止意图。
    if global_vars.is_system_stopped:
        server.should_exit = True
    return server


def run_api_server() -> None:
    """按开发 reload、安全模式多进程或生产单进程选择 Uvicorn 入口。"""
    global Server
    supervised = settings.DEV or settings.API_WORKERS > 1
    if supervised:
        if settings.DEV and settings.API_WORKERS > 1:
            raise UnsupportedProcessTopologyError(
                "Uvicorn reload 与多 worker 不能同时启用；"
                "开发模式请设置 API_WORKERS=1。"
            )
        Server = None
        uvicorn.run(
            APP_FACTORY,
            factory=True,
            host=settings.HOST,
            port=settings.PORT,
            reload=settings.DEV,
            workers=settings.API_WORKERS,
            timeout_graceful_shutdown=60,
        )
        return
    Server = create_server()
    Server.run()


def request_shutdown() -> None:
    """发布协作停止标志并请求 Uvicorn 退出"""
    global_vars.stop_system()
    if Server is not None:
        Server.should_exit = True


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
        request_shutdown()
        TrayIcon.stop()

    import pystray

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
    threading.Thread(target=TrayIcon.run, daemon=True).start()


def signal_handler(signum, frame):
    """
    信号处理函数，用于优雅停止服务
    """
    print(f"收到信号 {signum}，开始优雅停止服务...")
    request_shutdown()


def run_application() -> None:
    """初始化进程并启动 API 服务"""
    validate_process_topology(
        workers=settings.API_WORKERS,
        safe_mode=settings.MOVIEPILOT_SAFE_MODE,
    )
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    start_tray()
    run_api_server()


if __name__ == '__main__':
    run_application()
