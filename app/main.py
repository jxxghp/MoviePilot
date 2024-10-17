import multiprocessing
import os
import sys
import threading

import uvicorn as uvicorn
from PIL import Image
from uvicorn import Config

from app.factory import app
from app.utils.system import SystemUtils

# 禁用输出
if SystemUtils.is_frozen():
    sys.stdout = open(os.devnull, 'w')
    sys.stderr = open(os.devnull, 'w')

from app.core.config import settings
from app.db.init import init_db, update_db

# uvicorn服务
Server = uvicorn.Server(Config(app, host=settings.HOST, port=settings.PORT,
                               reload=settings.DEV, workers=multiprocessing.cpu_count(),
                               timeout_graceful_shutdown=5))


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


if __name__ == '__main__':
    # 启动托盘
    start_tray()
    # 初始化数据库
    init_db()
    # 更新数据库
    update_db()
    # 启动API服务
    Server.run()
