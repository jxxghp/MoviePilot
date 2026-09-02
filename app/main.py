import os
import shutil
import sys

# Windows 环境下手动注入 DLL 搜索路径
# Python 3.8+ 不再从环境变量 PATH 中自动加载 DLL。由于 psycopg 在 Free-Threading 版本中强制使用 C 扩展，
# 为避免因缺少底层 C 库导致运行失败，必须手动将 psql 所在目录加入 DLL 搜索路径。
if os.name == "nt":
    psql_exe = shutil.which("psql")
    if psql_exe:
        getattr(os, "add_dll_directory")(os.path.dirname(psql_exe))

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

import signal
import threading
from pathlib import Path
from typing import Optional

import setproctitle
import uvicorn as uvicorn
from PIL import Image
from uvicorn import Config

from app.adapters.system.host import SystemUtils
from app.adapters.system.stdio import configure_rotating_stdio

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
from app.runtime.settings import get_runtime_setting
from app.runtime.stop import runtime_stop_state

from app.runtime.topology import (
    UnsupportedProcessTopologyError,
    validate_process_topology,
)

setproctitle.setproctitle(get_runtime_setting('PROJECT_NAME'))


class MoviePilotServer(uvicorn.Server):
    """在 Uvicorn 开始优雅退出前发布应用协作停止标志"""

    def handle_exit(self, sig, frame) -> None:
        runtime_stop_state.stop_system()
        super().handle_exit(sig, frame)


APP_FACTORY = "app.factory:create_app"
Server: Optional[MoviePilotServer] = None


def create_server() -> MoviePilotServer:
    """创建不带 reload/multiprocess supervisor 的单进程生产服务器。"""
    server = MoviePilotServer(
        Config(
            app,
            host=get_runtime_setting('HOST'),
            port=get_runtime_setting('PORT'),
            reload=False,
            workers=1,
            timeout_graceful_shutdown=60,
        )
    )
    # 数据库准备阶段收到的信号早于 Server 物化，创建后必须继承既有停止意图。
    if runtime_stop_state.is_system_stopped:
        server.should_exit = True
    return server


def run_api_server() -> None:
    """按开发 reload、安全模式多进程或生产单进程选择 Uvicorn 入口。"""
    global Server
    supervised = get_runtime_setting('DEV') or get_runtime_setting('API_WORKERS') > 1
    if supervised:
        if get_runtime_setting('DEV') and get_runtime_setting('API_WORKERS') > 1:
            raise UnsupportedProcessTopologyError(
                "Uvicorn reload 与多 worker 不能同时启用；"
                "开发模式请设置 API_WORKERS=1。"
            )
        Server = None
        uvicorn.run(
            APP_FACTORY,
            factory=True,
            host=get_runtime_setting('HOST'),
            port=get_runtime_setting('PORT'),
            reload=get_runtime_setting('DEV'),
            # 运行插件及其恢复材料由插件生命周期管理，不属于宿主源码变更。
            reload_excludes=[
                str(get_runtime_setting('ROOT_PATH') / "app" / "plugins"),
                str(get_runtime_setting('CONFIG_PATH')),
            ]
            if get_runtime_setting('DEV')
            else None,
            workers=get_runtime_setting('API_WORKERS'),
            timeout_graceful_shutdown=60,
        )
        return
    Server = create_server()
    Server.run()


def request_shutdown() -> None:
    """发布协作停止标志并请求 Uvicorn 退出"""
    runtime_stop_state.stop_system()
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
        webbrowser.open(f"http://localhost:{get_runtime_setting('NGINX_PORT')}")

    def quit_app():
        """
        退出程序
        """
        request_shutdown()
        TrayIcon.stop()

    import pystray

    TrayIcon = pystray.Icon(
        get_runtime_setting('PROJECT_NAME'),
        icon=Image.open(get_runtime_setting('ROOT_PATH') / 'app.ico'),
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
        workers=get_runtime_setting('API_WORKERS'),
        safe_mode=get_runtime_setting('MOVIEPILOT_SAFE_MODE'),
    )
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    start_tray()
    run_api_server()


if __name__ == '__main__':
    run_application()
