"""pytest 全局引导：隔离 CONFIG_DIR、补 sites 垫片、建表、装载网络守卫。

引导与网络守卫均复用 ``app/testing`` 的共享 harness（与插件仓 conftest 同源），
引导逻辑只在 ``app/testing`` 维护一处。
"""
import sys

import pytest

# 必须早于首个牵入 app.runtime.config 的 import（app.db / app.chain.* 都会牵入）：引擎本身已惰性，
# import app.db 不再连库，但 settings 在 import 期就把 CONFIG_DIR 读进字段并建好配置目录，之后
# 改环境变量已经晚了。prepare_backend 内部先隔离 CONFIG_DIR、补 app.application.site.sites 垫片，
# 再建表。app/testing 仅依赖标准库、import 不触发 app.*，故此处先 import 再调用是安全的。
from app.testing.bootstrap import prepare_backend

prepare_backend()

# 复用共享 autouse 网络守卫；同一实现亦供各插件仓 conftest import 复用，避免逐仓维护
from app.testing.network_guard import block_real_network  # noqa: E402,F401


@pytest.fixture(autouse=True)
def configure_plugin_system_services():
    """为绕过完整启动流程的单元测试装配真实插件系统适配器。"""
    from app.adapters.external.market import (
        PluginHelper,
        VERSION_BACKWARD_COMPATIBLE_FLAGS,
    )
    from app.adapters.external.plugin.client import PluginMarketClient
    from app.adapters.system.plugin.dependency import PluginDependencyInstaller
    from app.adapters.system.plugin.package import PluginPackageManager
    from app.runtime.extensions.plugin.system import (
        PluginSystemServices,
        configure_plugin_system,
        reset_plugin_system,
    )

    helper = PluginHelper()
    configure_plugin_system(PluginSystemServices(
        market=PluginMarketClient(helper),
        package=PluginPackageManager(helper),
        dependency=PluginDependencyInstaller(helper),
        compatible_flags=lambda flag: (
            [flag] + VERSION_BACKWARD_COMPATIBLE_FLAGS.get(flag, [])
            if flag else []
        ),
        frozen=lambda: False,
    ))
    yield
    reset_plugin_system()


class DbHarness:
    """真实数据库会话的测试载具。

    ``prepare_backend`` 已把 CONFIG_DIR 指向临时目录并建好表，操作的是一次性数据库；
    但同一次 pytest 会话内所有用例共用这一个库，因此清理必须精确到行——按主键水位回收
    用例新增的数据，而不是 truncate 整表，否则会连带删掉其他用例依赖的数据。

    水位法同时覆盖「被测代码自己写入的行」：只要在写入前登记过该表，其后新增的行
    都会被回收，测试不必持有每一个模型实例的句柄。
    """

    def __init__(self, session):
        self.session = session
        self._watermarks = {}

    def watermark(self, *models) -> None:
        """
        登记若干表的当前最大主键，用例结束时删除其后新增的全部行。
        :param models: 需要纳入回收的模型类
        """
        from sqlalchemy import func, select

        for model in models:
            if model in self._watermarks:
                continue
            current = self.session.execute(select(func.max(model.id))).scalar()
            self._watermarks[model] = current or 0

    def add(self, *rows):
        """
        写入若干行并提交，返回单行或行列表。

        写入前自动登记水位，因此这些行以及被测代码后续新增的同表行都会被回收。
        :param rows: 待写入的模型实例
        """
        self.watermark(*{type(row) for row in rows})
        for row in rows:
            self.session.add(row)
        self.session.commit()
        return rows[0] if len(rows) == 1 else list(rows)

    def cleanup(self) -> None:
        """按水位删除本用例新增的全部行。"""
        from sqlalchemy import delete

        # 用例可能因约束冲突等原因让事务处于待回滚状态，此时任何语句都会被拒绝；
        # 先回滚再清理，否则清理会整体失效、数据泄漏到后续用例
        try:
            self.session.rollback()
        except Exception:  # noqa: BLE001  会话已不可用时也要继续尝试清理
            pass

        for model, mark in self._watermarks.items():
            try:
                self.session.execute(delete(model).where(model.id > mark))
                self.session.commit()
            except Exception:  # noqa: BLE001  清理失败不应掩盖用例本身的断言结果
                self.session.rollback()


@pytest.fixture
def db():
    """
    提供真实数据库会话载具，用例结束按主键水位回收新增数据。

    数据库查询方法的行为（过滤、排序、分页、去重）无法用替身验证——替身只能证明
    「调用了什么」，证明不了「查回了什么」，而 1.x Query 到 2.0 select 的改写恰恰
    只可能在后者上出偏差。
    """
    from app.db.session import ScopedSession

    session = ScopedSession()
    harness = DbHarness(session)
    try:
        yield harness
    finally:
        harness.cleanup()
        session.close()


@pytest.fixture
def frozen_now(monkeypatch):
    """
    冻结指定模块看到的 ``time.time()``，其余时间函数原样透传标准库。

    形如 ``date >= now - 86400 * days`` 的时间窗查询，窗口起点要到调用那一刻才算得出来，
    不冻结就没法把数据精确摆在窗口起点上——而边界恰恰是 ``>=`` 与 ``>`` 唯一的分界，
    数据不压在边界上，比较符写错也查不出来。

    :return: ``freeze(module) -> float``，冻结该模块的时钟并返回冻结时刻的时间戳
    """
    import time as real_time

    class _FrozenClock:
        """只冻结 ``time()``，``localtime``/``strftime`` 等仍走标准库。"""

        def __init__(self, now: float):
            self.now = now

        def time(self) -> float:
            return self.now

        def __getattr__(self, name):
            return getattr(real_time, name)

    def freeze(module) -> float:
        """
        把模块内的 ``time`` 名字换成冻结时钟。
        :param module: 被测代码所在模块（其内以 ``time.time()`` 取当前时刻）
        :return: 冻结时刻的时间戳
        """
        clock = _FrozenClock(real_time.time())
        monkeypatch.setattr(module, "time", clock)
        return clock.now

    return freeze


def _report_session_cleanup_error(session, name: str, err: Exception) -> None:
    """记录收尾错误；原测试绿色时将会话标记为失败。"""
    sys.stderr.write(f"\npytest session cleanup failed: {name}: {err!r}\n")
    if session.exitstatus == 0:
        session.exitstatus = 1


def pytest_sessionfinish(session, exitstatus):
    """释放测试过程中按需创建的全局后台资源，避免解释器退出时等待非 daemon worker。"""
    try:
        from app.agent.tools.base import shutdown_blocking_executors

        shutdown_blocking_executors(cancel_futures=True)
    except Exception as err:
        _report_session_cleanup_error(session, "agent blocking executors", err)

    try:
        from app.runtime.thread import ThreadHelper

        helper = ThreadHelper.get_existing_instance()
        if helper:
            helper.shutdown()
    except Exception as err:
        _report_session_cleanup_error(session, "thread helper", err)

    try:
        from app.application.messaging.message import stop_message

        stop_message()
    except Exception as err:
        _report_session_cleanup_error(session, "message service", err)

    try:
        from app.runtime.log import LoggerManager

        LoggerManager.shutdown()
    except Exception as err:
        _report_session_cleanup_error(session, "logger manager", err)
