"""
引擎的惰性创建。

此前引擎在 import 期创建：`import app.db` 就会按 settings 连库、建出 user.db、SQLite
还会去设一次 WAL——仅仅把这个包 import 进来就有副作用，且违反了「隔离 CONFIG_DIR 必须
早于它」时不会报错，只会静默写进真实的 user.db。

惰性化消掉的是这个副作用（排序约束本身仍在，见 engine 模块注释），代价是引入了新的
正确性问题：首次访问的并发。这个项目有上百个
调度线程，双重检查一旦写错，会创建出多个引擎、各自持一份连接池，额度核算随之失真。
这类 bug 在单线程测试里永远不会暴露，所以这里显式并发压它。
"""
import asyncio
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock

import pytest

from app.runtime.config import global_vars, settings
from app.db import decorators as decorators_module
from app.db import engine as engine_module
from app.db import session as session_module


@pytest.fixture
def _reset_engines():
    """复原引擎缓存，避免用例之间相互影响。

    只做「存档—还原」，不负责释放：用例必须自行给 ``_get_database_engine`` 打桩，
    绝不能在这里落下真引擎——还原会把它从槽里丢掉，那条连接便再无人 dispose。
    """
    saved_sync, saved_async = engine_module._sync_engine, engine_module._async_engine
    yield
    engine_module._sync_engine, engine_module._async_engine = saved_sync, saved_async


@pytest.fixture
def _reset_pooled_engines():
    """复原按事件循环缓存的池化引擎。"""
    saved = dict(session_module._pooled_async_engines)
    yield
    session_module._pooled_async_engines.clear()
    session_module._pooled_async_engines.update(saved)


def test_engine_is_not_created_on_import():
    """
    仅 import 不得创建引擎——这是「独立可测」的全部意义所在。

    必须用子进程验证：当前进程早被其它用例触发过引擎创建了。
    """
    code = (
        "import app.db, app.db.engine as e; "
        "print('CREATED' if e._sync_engine is not None or e._async_engine is not None "
        "else 'LAZY')"
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", "CONFIG_DIR": tmp,
                                  "PYTHONPATH": "."}, timeout=180)
    assert "LAZY" in out.stdout, f"import 期即创建了引擎：{out.stdout}{out.stderr[-800:]}"


def test_importing_legacy_factory_names_does_not_create_engine():
    """
    `from app.db import SessionFactory` 这类旧写法也不得连带创建引擎。

    这三个名字是转发函数而非 sessionmaker 实例，正是为了让「导入」和「创建」分开：
    若改回靠模块级 __getattr__ 解析，每个导入方都会在 import 期把引擎建出来。
    """
    code = (
        "from app.db import SessionFactory, AsyncSessionFactory, ScopedSession; "
        "import app.db.engine as e; "
        "print('CREATED' if e._sync_engine is not None or e._async_engine is not None "
        "else 'LAZY')"
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", "CONFIG_DIR": tmp,
                                  "PYTHONPATH": "."}, timeout=180)
    assert "LAZY" in out.stdout, f"导入会话工厂即创建了引擎：{out.stdout}{out.stderr[-800:]}"


def test_bootstrap_atexit_cleanup_does_not_create_an_engine():
    """
    测试引导的退出清理不得为了 dispose 而把引擎创建出来。

    `isolate_config_dir` 注册的 atexit 回调原本写作 `app.db.Engine.dispose()`——`Engine`
    是惰性解析的属性，取它本身就会**创建**引擎。于是一个只 import 过 `app.db` 的进程会在
    解释器关停时凭空连一次库、SQLite 还要再设一遍 journal mode，全部只为随后把它 dispose。

    必须用子进程，且断言的是「连库的副作用没有发生」而不是引擎槽位：回调在解释器关停期
    才执行，那时已经没有任何代码能跑断言了，能留下的证据只有 stdout。
    也必须让子进程自己调 isolate_config_dir()——它只在真的新建了临时目录时才注册回调，
    预先把 CONFIG_DIR 塞进环境会让它直接返回、根本不注册 atexit，用例便成了空跑。
    """
    code = (
        "from app.testing.bootstrap import isolate_config_dir; "
        "isolate_config_dir(); "
        "import app.db; "
        "print('IMPORTED')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "."}, timeout=180)
    assert "IMPORTED" in out.stdout, f"子进程没跑到底：{out.stdout}{out.stderr[-800:]}"
    assert "journal mode set to" not in out.stdout, (
        f"退出清理凭空建了个引擎、连了一次库：{out.stdout}{out.stderr[-800:]}"
    )


def test_db_query_decorator_resolves_session_at_call_time(monkeypatch):
    """
    装饰器必须能在调用期取到会话。

    守的是一个真实踩过的坑：曾试图用模块级 __getattr__（PEP 562）把 ScopedSession
    延迟到运行期解析，但 __getattr__ 只对「对模块对象取属性」生效，装饰器函数体里的
    裸名字 ScopedSession 是**全局名字查找**，只查模块 __dict__ 和 builtins，永远走不
    到 __getattr__ —— 结果是运行期 NameError。它只在真正调用到某个 Oper 时才炸，
    import 与单测都照常绿灯，因此必须显式钉住。
    """
    fake_session = MagicMock()
    # 替身打在 session 模块的工厂上，而不是 decorators.ScopedSession：后者会把
    # 名字直接塞进 decorators 的 __dict__，反而掩盖「这个名字本来就该在」的缺陷。
    monkeypatch.setattr(session_module, "get_scoped_session", lambda: (lambda: fake_session))

    @decorators_module.db_query
    def _fetch(db=None):
        """装饰器未拿到会话时应自行创建一个并塞回 db 位置。"""
        return db

    # 按各 Oper 的常态调用：db 显式传 None，由装饰器补上会话
    assert _fetch(db=None) is fake_session
    fake_session.close.assert_called_once()


def test_concurrent_first_access_creates_exactly_one_engine(_reset_engines, monkeypatch):
    """
    多线程同时首次取引擎，只能创建出一个实例。

    创建多个意味着每个都带一份连接池：实际连接数是额度核算的数倍，而校验对此
    一无所知——正是这次修复想避免的那类问题。
    """
    engine_module._sync_engine = None
    created = []
    barrier = threading.Barrier(16)

    def slow_factory(**_kwargs):
        """放大创建耗时，把竞态窗口撑开到必定命中。"""
        time.sleep(0.02)
        marker = object()
        created.append(marker)
        return marker

    monkeypatch.setattr(engine_module, "_get_database_engine", slow_factory)
    got = []

    def worker():
        """所有线程在同一时刻发起首次访问。"""
        barrier.wait()
        got.append(engine_module.get_engine())

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created) == 1, f"并发首次访问创建了 {len(created)} 个引擎"
    assert len({id(g) for g in got}) == 1, "不同线程拿到了不同的引擎实例"


def test_repeated_access_reuses_the_same_engine(_reset_engines, monkeypatch):
    """
    后续访问必须复用，而不是每次重建。
    """
    engine_module._sync_engine = None
    calls = []
    monkeypatch.setattr(engine_module, "_get_database_engine",
                        lambda **kw: calls.append(kw) or object())

    first = engine_module.get_engine()
    second = engine_module.get_engine()

    assert first is second
    assert len(calls) == 1


def test_async_engine_has_its_own_lazy_slot(_reset_engines, monkeypatch):
    """
    同步与异步引擎各自独立惰性化，取其中一个不应连带创建另一个。
    """
    engine_module._sync_engine = engine_module._async_engine = None
    monkeypatch.setattr(engine_module, "_get_database_engine", lambda **kw: object())

    engine_module.get_engine()

    assert engine_module._sync_engine is not None
    assert engine_module._async_engine is None, "取同步引擎连带创建了异步引擎"


def test_pooled_path_does_not_create_the_global_async_engine(
        _reset_engines, _reset_pooled_engines, monkeypatch):
    """
    常驻循环走池化引擎时，全局异步引擎必须原封不动地留在「未创建」状态。

    钉的是一处已经踩过的实现：async_session_scope 曾用
    `engine is not get_global_async_engine()` 反推是否池化——这个比较**本身**就把被比较的
    引擎创建了出来。它倒不至于多占连接——全局异步引擎用的是 NullPool，持有 0 条连接；
    真正的代价是第一个异步请求会在事件循环内部去抢引擎创建锁，把本该无锁的热路径变成
    有锁的，而这个引擎在常驻循环下从头到尾无人使用。这类问题不会让任何断言变红，只能显式压。
    """
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_TYPE", "QueuePool", raising=False)
    engine_module._async_engine = None

    def _boom():
        """任何对全局异步引擎的获取都是失败信号。"""
        raise AssertionError("池化路径获取了全局异步引擎")

    # 打在 session 模块的名字上：_resolve_async_engine 用的是它自己 __dict__ 里的这个名字
    monkeypatch.setattr(session_module, "get_global_async_engine", _boom)

    async def run():
        """在「当前循环即常驻循环」的前提下真的走一遍会话作用域。"""
        global_vars.CURRENT_EVENT_LOOP = asyncio.get_running_loop()
        session_module._pooled_async_engines.clear()
        try:
            async with session_module.async_session_scope():
                pass
        finally:
            # 用真引擎（不给工厂打桩）才能验证会话确实能建起来；建了就得自己释放
            for pooled in session_module._pooled_async_engines.values():
                await pooled.dispose()
            session_module._pooled_async_engines.clear()

    saved_loop = global_vars.CURRENT_EVENT_LOOP
    try:
        asyncio.run(run())
    finally:
        global_vars.CURRENT_EVENT_LOOP = saved_loop

    assert engine_module._async_engine is None, "池化路径把全局异步引擎创建了出来"


def test_engine_module_exposes_no_legacy_names(_reset_engines, monkeypatch):
    """
    app.db.engine 不再解析 Engine / AsyncEngine 两个旧名字。

    这两个名字的对外契约是 `app.db.Engine`（由 app/db/__init__.py 的 __getattr__ 提供）。
    app.db.engine 这个模块是拆分时才出现的，仓库外不可能有代码依赖它，实现模块上那份
    同名转发因此是纯冗余——两处独立实现同一个契约，改一处漏一处就会各取到一个引擎。
    仓库内一律用 get_engine() / get_global_async_engine()。

    工厂打桩而不是让它建真引擎：_reset_engines 还原槽位时会把真引擎丢掉，
    那条连接就再没人 dispose 了。
    """
    engine_module._sync_engine = engine_module._async_engine = None
    monkeypatch.setattr(engine_module, "_get_database_engine", lambda **kw: object())

    with pytest.raises(AttributeError):
        _ = engine_module.Engine
    with pytest.raises(AttributeError):
        _ = engine_module.AsyncEngine
    # 取过之后引擎槽位仍是空的：属性访问没有绕开 getter 把引擎建出来
    assert engine_module._sync_engine is None
    assert engine_module._async_engine is None


def test_package_entry_resolves_legacy_names(_reset_engines, monkeypatch):
    """
    app.db.Engine / app.db.AsyncEngine 仍解析到与 getter 同一个引擎。

    这才是真正的对外契约：仓库外的插件按这两个名字取引擎，建表、Alembic 迁移、连接
    诊断这类用途确实需要引擎对象本身，装饰器覆盖不到。上一条用例删掉了实现模块上的
    冗余转发，这条钉住包入口那份**不能**跟着删。

    与惰性不冲突：属性访问发生在运行期，而不是 import 期。
    """
    import app.db as db_package

    engine_module._sync_engine = engine_module._async_engine = None
    monkeypatch.setattr(engine_module, "_get_database_engine", lambda **kw: object())

    assert db_package.Engine is engine_module.get_engine()
    assert db_package.AsyncEngine is engine_module.get_global_async_engine()


def test_package_entry_unknown_attribute_still_raises():
    """
    包入口的模块级 __getattr__ 不能吞掉拼写错误。
    """
    import app.db as db_package

    with pytest.raises(AttributeError):
        _ = db_package.NoSuchThing
