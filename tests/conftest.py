"""pytest 全局引导：隔离 CONFIG_DIR、补 sites 垫片、建表、装载网络守卫。

引导与网络守卫均复用 ``app/testing`` 的共享 harness（与插件仓 conftest 同源），
引导逻辑只在 ``app/testing`` 维护一处。
"""
import sys

# 必须早于首个 import app.db（其在 import 期即按 CONFIG_PATH 连库）：prepare_backend 内部
# 先隔离 CONFIG_DIR、补 app.helper.sites 垫片，再建表。app/testing 仅依赖标准库、import 不连库，
# 故此处先 import 再调用是安全的。
from app.testing.bootstrap import prepare_backend

prepare_backend()

# 复用共享 autouse 网络守卫；同一实现亦供各插件仓 conftest import 复用，避免逐仓维护
from app.testing.network_guard import block_real_network  # noqa: E402,F401


def _report_session_cleanup_error(name: str, err: Exception) -> None:
    """测试收尾清理失败只记录诊断，不覆盖原始 pytest 退出状态。"""
    sys.stderr.write(f"\npytest session cleanup failed: {name}: {err!r}\n")


def pytest_sessionfinish(session, exitstatus):
    """释放测试过程中按需创建的全局后台资源，避免解释器退出时等待非 daemon worker。"""
    try:
        from app.agent.tools.base import shutdown_blocking_executors

        shutdown_blocking_executors(cancel_futures=True)
    except Exception as err:
        _report_session_cleanup_error("agent blocking executors", err)

    try:
        from app.helper.thread import ThreadHelper
        from app.utils.singleton import Singleton

        helper = Singleton._instances.get((ThreadHelper, (), frozenset()))
        if helper:
            helper.shutdown()
    except Exception as err:
        _report_session_cleanup_error("thread helper", err)

    try:
        from app.log import LoggerManager

        LoggerManager.shutdown()
    except Exception as err:
        _report_session_cleanup_error("logger manager", err)
