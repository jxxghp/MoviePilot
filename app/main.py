import multiprocessing
import os
import setproctitle
import signal
import sys
import threading
from pathlib import Path

import uvicorn as uvicorn
from PIL import Image
from uvicorn import Config

from app.utils.stdio import configure_rotating_stdio
from app.utils.system import SystemUtils

# 禁用输出
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
from app.core.config import global_vars, settings
from app.db.init import init_db, update_db

# 设置进程名
setproctitle.setproctitle(settings.PROJECT_NAME)


class MoviePilotServer(uvicorn.Server):
    """在 Uvicorn 开始优雅退出前发布应用协作停止标志"""

    def handle_exit(self, sig, frame) -> None:
        global_vars.stop_system()
        super().handle_exit(sig, frame)


# uvicorn服务
Server = MoviePilotServer(Config(app, host=settings.HOST, port=settings.PORT,
                                 reload=settings.DEV, workers=multiprocessing.cpu_count() * 2 + 1,
                                 timeout_graceful_shutdown=60))


def request_shutdown() -> None:
    """发布协作停止标志并请求 Uvicorn 退出"""
    global_vars.stop_system()
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


def signal_handler(signum, frame):
    """
    信号处理函数，用于优雅停止服务
    """
    print(f"收到信号 {signum}，开始优雅停止服务...")
    request_shutdown()


def run_application() -> None:
    """初始化进程并启动 API 服务"""
    # 注册信号处理器
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # 启动托盘
    start_tray()
    # 初始化数据库
    init_db()
    # 更新数据库
    update_db()
    # 启动API服务
    Server.run()


if __name__ == '__main__':
    run_application()
