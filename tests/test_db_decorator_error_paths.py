"""
事务装饰器的异常与回滚路径测试。

四个装饰器的正常路径处处都在被间接使用，出错路径却一次都没被验证过——而
「事务中途抛异常」恰恰是惰性引擎、连接额度核算、按事件循环池化这几项改动
共同的失败模式：出错时是否真的回滚、异常是否原样上抛、会话与配额是否仍被
释放，任何一条失守都不会让别的用例变红，只会在生产上表现为脏事务、被顶替
的异常，或再也拿不回来的连接配额。

这里全部用替身构造会话：验的是装饰器的控制流（谁被调用、谁没被调用、异常
怎么传），不是 SQL 行为，真实会话反而会把这些信号淹掉。
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import decorators as decorators_module


class _BusinessError(Exception):
    """被包装函数抛出的业务异常，用于验证它能原样抵达调用方。"""


class _FakeScope:
    """
    async_session_scope 的替身，记录进入与退出次数。

    退出必须能被单独断言：回退路径的全局配额释放绑定在 __aexit__ 上，
    只 close 会话而不退出上下文会让配额永不归还。
    """

    def __init__(self, session):
        self.session = session
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self.session

    async def __aexit__(self, *_exc):
        self.exited += 1
        return False


class _FailingScope(_FakeScope):
    """
    退出时抛异常的作用域替身：模拟释放阶段才发现连接已断。

    异步侧的释放走 __aexit__ 而不是 close()，所以故障也只能从这里注入。
    """

    def __init__(self, session, error):
        super().__init__(session)
        self.error = error

    async def __aexit__(self, *_exc):
        self.exited += 1
        raise self.error


def _sync_session() -> MagicMock:
    """
    造一个能被 _get_args_db 认作调用方会话的同步会话替身。

    必须带 spec：装饰器用 isinstance(arg, Session) 判定「调用方是否已传会话」，
    裸 MagicMock 会被当成没传。
    """
    return MagicMock(spec=Session)


def _async_session() -> MagicMock:
    """
    造一个异步会话替身，commit/rollback/close 自动是 AsyncMock（可 await）。
    """
    return MagicMock(spec=AsyncSession)


def _install_scope(monkeypatch, session) -> _FakeScope:
    """
    把 async_session_scope 换成返回替身作用域，返回该作用域以便断言。
    :param monkeypatch: pytest 的 monkeypatch
    :param session: 作用域内交出的会话替身
    """
    scope = _FakeScope(session)
    monkeypatch.setattr(decorators_module, "async_session_scope", lambda: scope)
    return scope


def _install_failing_scope(monkeypatch, session, error) -> _FailingScope:
    """
    同上，但作用域退出时抛出指定异常，用于验证释放故障的处理。
    :param monkeypatch: pytest 的 monkeypatch
    :param session: 作用域内交出的会话替身
    :param error: __aexit__ 抛出的异常
    """
    scope = _FailingScope(session, error)
    monkeypatch.setattr(decorators_module, "async_session_scope", lambda: scope)
    return scope


def _capture_logger_errors(monkeypatch) -> list:
    """
    截获 logger.error 的消息，返回随调用不断追加的列表。
    """
    logged = []
    monkeypatch.setattr(decorators_module.logger, "error", lambda msg, *a, **kw: logged.append(msg))
    return logged


# ==================== db_update：同步更新 ====================

def test_db_update_rolls_back_and_skips_commit_on_error():
    """
    被包装函数抛异常时必须回滚，且绝不能提交。

    漏掉回滚会把半截事务留在会话里：同一线程的 scoped_session 会被后续操作
    继续复用，脏数据要么被下一次无关的 commit 顺手带进库，要么让后续语句
    全部撞在「事务已中止」上。
    """
    db = _sync_session()

    @decorators_module.db_update
    def _write(db=None):
        """必定失败的更新。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        _write(db=db)

    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_db_update_reraises_the_original_exception_object():
    """
    原异常必须原样上抛：类型与实例都不变，不被包装也不被吞。

    调用方靠异常类型分流（唯一约束冲突要重试、参数错误要报错），一旦被换成
    别的类型，上层的 except 就再也匹配不上。
    """
    db = _sync_session()
    boom = _BusinessError("boom")

    @decorators_module.db_update
    def _write(db=None):
        """抛出一个可辨认的异常实例。"""
        raise boom

    with pytest.raises(_BusinessError) as excinfo:
        _write(db=db)

    assert excinfo.value is boom, "上抛的不是原始异常实例"


def test_db_update_closes_self_created_session_on_error(monkeypatch):
    """
    装饰器自建的会话，异常路径下同样要关闭。

    释放写在 finally 里就是为了这个：把它挪进 try 的尾部，正常路径照常绿灯，
    每一次失败的写入却都会漏掉一条连接。
    """
    db = _sync_session()
    monkeypatch.setattr(decorators_module, "ScopedSession", lambda: db)

    @decorators_module.db_update
    def _write(db=None):
        """必定失败的更新。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        _write(db=None)

    db.close.assert_called_once()


def test_db_update_does_not_close_caller_session_on_error():
    """
    调用方传入的会话，异常路径下不得关闭——不是装饰器创建的，就无权释放。

    关掉别人的会话比泄漏更糟：调用方后面还要在同一个会话上做别的事，
    而它已经被这次失败连带关掉了。
    """
    db = _sync_session()

    @decorators_module.db_update
    def _write(db=None):
        """必定失败的更新。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        _write(db=db)

    db.close.assert_not_called()


# ==================== async_db_update：异步更新 ====================

@pytest.mark.asyncio
async def test_async_db_update_rolls_back_and_skips_commit_on_error():
    """
    异步更新出错时同样必须回滚、绝不提交（且回滚是 await 的）。
    """
    db = _async_session()

    @decorators_module.async_db_update
    async def _write(db=None):
        """必定失败的异步更新。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        await _write(db=db)

    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_db_update_reraises_the_original_exception_object():
    """
    异步路径的原异常同样要原样上抛，类型与实例都不变。
    """
    db = _async_session()
    boom = _BusinessError("boom")

    @decorators_module.async_db_update
    async def _write(db=None):
        """抛出一个可辨认的异常实例。"""
        raise boom

    with pytest.raises(_BusinessError) as excinfo:
        await _write(db=db)

    assert excinfo.value is boom, "上抛的不是原始异常实例"


@pytest.mark.asyncio
async def test_async_db_update_exits_scope_on_error(monkeypatch):
    """
    自建会话时，异常路径下必须退出会话作用域，而不是只关会话。

    回退路径（非常驻循环）的全局连接配额是在 async_session_scope 的 finally 里
    归还的，只有走 __aexit__ 才会触发。写成 `await db.close()` 时正常路径与异常
    路径的断言都照样绿——会话确实关了——但每一次失败都会永久吃掉一个配额名额，
    攒够上限后整个进程的异步数据库访问一起饿死。
    """
    db = _async_session()
    scope = _install_scope(monkeypatch, db)

    @decorators_module.async_db_update
    async def _write(db=None):
        """必定失败的异步更新。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        await _write(db=None)

    assert scope.exited == 1, "异常路径没有退出会话作用域，配额不会归还"


@pytest.mark.asyncio
async def test_async_db_update_does_not_release_caller_session_on_error(monkeypatch):
    """
    调用方传入异步会话时，异常路径下既不建作用域也不关它的会话。
    """
    db = _async_session()

    def _boom():
        """任何建作用域的行为都是失败信号。"""
        raise AssertionError("调用方已传入会话，装饰器不应再建作用域")

    monkeypatch.setattr(decorators_module, "async_session_scope", _boom)

    @decorators_module.async_db_update
    async def _write(db=None):
        """必定失败的异步更新。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        await _write(db=db)

    db.close.assert_not_awaited()


# ==================== db_query：同步查询 ====================

def test_db_query_does_not_touch_transaction_on_error():
    """
    查询装饰器不管事务：出错时既不提交也不回滚。

    替调用方回滚会把它自己那段尚未提交的事务一并抹掉——查询只是借了会话，
    无权处置会话上正在进行的事务。
    """
    db = _sync_session()

    @decorators_module.db_query
    def _read(db=None):
        """必定失败的查询。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        _read(db=db)

    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def test_db_query_reraises_the_original_exception_object():
    """
    查询出错时原异常原样上抛，类型与实例都不变。
    """
    db = _sync_session()
    boom = _BusinessError("boom")

    @decorators_module.db_query
    def _read(db=None):
        """抛出一个可辨认的异常实例。"""
        raise boom

    with pytest.raises(_BusinessError) as excinfo:
        _read(db=db)

    assert excinfo.value is boom, "上抛的不是原始异常实例"


def test_db_query_closes_self_created_session_on_error(monkeypatch):
    """
    自建会话的查询，异常路径下同样要关闭会话。
    """
    db = _sync_session()
    monkeypatch.setattr(decorators_module, "ScopedSession", lambda: db)

    @decorators_module.db_query
    def _read(db=None):
        """必定失败的查询。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        _read(db=None)

    db.close.assert_called_once()


# ==================== async_db_query：异步查询 ====================

@pytest.mark.asyncio
async def test_async_db_query_does_not_touch_transaction_on_error():
    """
    异步查询出错时同样既不提交也不回滚。
    """
    db = _async_session()

    @decorators_module.async_db_query
    async def _read(db=None):
        """必定失败的异步查询。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        await _read(db=db)

    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_db_query_reraises_the_original_exception_object():
    """
    异步查询出错时原异常原样上抛，类型与实例都不变。
    """
    db = _async_session()
    boom = _BusinessError("boom")

    @decorators_module.async_db_query
    async def _read(db=None):
        """抛出一个可辨认的异常实例。"""
        raise boom

    with pytest.raises(_BusinessError) as excinfo:
        await _read(db=db)

    assert excinfo.value is boom, "上抛的不是原始异常实例"


@pytest.mark.asyncio
async def test_async_db_query_exits_scope_on_error(monkeypatch):
    """
    自建会话的异步查询，异常路径下必须退出会话作用域（配额同样绑在 __aexit__ 上）。
    """
    db = _async_session()
    scope = _install_scope(monkeypatch, db)

    @decorators_module.async_db_query
    async def _read(db=None):
        """必定失败的异步查询。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        await _read(db=None)

    assert scope.exited == 1, "异常路径没有退出会话作用域，配额不会归还"


# ==================== 回滚本身失败 ====================

def test_db_update_rollback_failure_does_not_replace_original_error():
    """
    回滚自身失败时，调用方仍须收到原始业务异常。

    `except: db.rollback(); raise err` 的写法里，回滚一抛错就直接顶替了原始异常：
    连接断开、事务已失效这类收尾故障恰恰最容易在「出错之后」发生，于是排障时看到的
    永远是「connection reset」，真正的业务异常连类型都被换掉，调用方按类型分流的
    except 也一并失配。
    """
    db = _sync_session()
    db.rollback.side_effect = RuntimeError("connection reset")
    boom = _BusinessError("boom")

    @decorators_module.db_update
    def _write(db=None):
        """抛出一个可辨认的异常实例。"""
        raise boom

    with pytest.raises(_BusinessError) as excinfo:
        _write(db=db)

    assert excinfo.value is boom, "回滚失败顶替了原始异常"


def test_db_update_logs_rollback_failure(monkeypatch):
    """
    回滚失败不能被静默吞掉：原始异常照常上抛，回滚故障要留下记录。

    否则「连接已断」这个同样重要的信号会彻底消失——只保原始异常而不记回滚故障，
    等于用一个盲区换掉另一个盲区。
    """
    logged = _capture_logger_errors(monkeypatch)
    db = _sync_session()
    db.rollback.side_effect = RuntimeError("connection reset")

    @decorators_module.db_update
    def _write(db=None):
        """必定失败的更新。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        _write(db=db)

    assert any("connection reset" in str(msg) for msg in logged), \
        f"回滚失败被静默吞掉，未留下任何记录：{logged}"


@pytest.mark.asyncio
async def test_async_db_update_rollback_failure_does_not_replace_original_error():
    """
    异步回滚自身失败时，调用方同样必须收到原始业务异常。
    """
    db = _async_session()
    db.rollback.side_effect = RuntimeError("connection reset")
    boom = _BusinessError("boom")

    @decorators_module.async_db_update
    async def _write(db=None):
        """抛出一个可辨认的异常实例。"""
        raise boom

    with pytest.raises(_BusinessError) as excinfo:
        await _write(db=db)

    assert excinfo.value is boom, "回滚失败顶替了原始异常"


# ==================== 释放本身失败 ====================
#
# 释放（同步的 db.close()、异步的 _scope.__aexit__()）写在 finally 里，裸写时它一抛错
# 就成了整个 finally 的出口：异常路径下顶替掉正在传播的业务异常，成功路径下把一次已经
# 提交的写入变成调用方眼里的失败。三组断言分别钉住三件事——原始异常不被顶替、释放故障
# 不被静默吞掉、成功路径的返回值不被释放故障拦截。
#
# 第三条是本次修改**新引入**的行为（改前：func() 成功而 close() 失败，调用方收到异常；
# 改后：静默拿到返回值，故障只进日志）。它是刻意的取舍而非疏漏，因此必须有用例钉住，
# 否则下一个人会把它当 bug「修」回去。
#
# 释放只发生在装饰器自建会话时，所以这些用例一律走自建路径（monkeypatch 掉会话来源）。

def test_db_update_close_failure_does_not_replace_original_error(monkeypatch):
    """
    关闭会话失败时，调用方仍须收到原始业务异常。

    与回滚同理：连接已断这类故障最容易出现在收尾阶段，裸写 db.close() 时它一抛错，
    调用方看到的就只剩「connection reset」，业务异常连类型都被换掉。
    """
    db = _sync_session()
    db.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(decorators_module, "ScopedSession", lambda: db)
    boom = _BusinessError("boom")

    @decorators_module.db_update
    def _write(db=None):
        """抛出一个可辨认的异常实例。"""
        raise boom

    with pytest.raises(_BusinessError) as excinfo:
        _write(db=None)

    assert excinfo.value is boom, "关闭会话失败顶替了原始异常"


def test_db_update_logs_close_failure(monkeypatch):
    """
    关闭会话失败不能被静默吞掉：不上抛，但要留下记录。

    释放故障是连接池异常的先兆信号，既不上抛又不记录等于让它彻底消失。
    """
    logged = _capture_logger_errors(monkeypatch)
    db = _sync_session()
    db.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(decorators_module, "ScopedSession", lambda: db)

    @decorators_module.db_update
    def _write(db=None):
        """必定失败的更新。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        _write(db=None)

    assert any("close failed" in str(msg) for msg in logged), \
        f"关闭会话失败被静默吞掉，未留下任何记录：{logged}"


def test_db_update_close_failure_does_not_break_success_path(monkeypatch):
    """
    业务成功而释放失败时，调用方必须正常拿到返回值（本次修改新引入的行为）。

    此时事务已经 commit、数据确实落库了，把释放故障升级成调用方的异常只会让一次成功的
    写入看起来像失败，诱使上层重试、重复提交。故障降级为日志是刻意的取舍。
    """
    logged = _capture_logger_errors(monkeypatch)
    db = _sync_session()
    db.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(decorators_module, "ScopedSession", lambda: db)

    @decorators_module.db_update
    def _write(db=None):
        """成功的更新。"""
        return "written"

    assert _write(db=None) == "written", "释放失败把成功的写入变成了异常"
    db.commit.assert_called_once()
    assert any("close failed" in str(msg) for msg in logged), \
        f"释放故障既没上抛也没记录，等于彻底消失：{logged}"


@pytest.mark.asyncio
async def test_async_db_update_scope_exit_failure_does_not_replace_original_error(monkeypatch):
    """
    异步更新：作用域退出失败时，调用方仍须收到原始业务异常。
    """
    db = _async_session()
    _install_failing_scope(monkeypatch, db, RuntimeError("exit failed"))
    boom = _BusinessError("boom")

    @decorators_module.async_db_update
    async def _write(db=None):
        """抛出一个可辨认的异常实例。"""
        raise boom

    with pytest.raises(_BusinessError) as excinfo:
        await _write(db=None)

    assert excinfo.value is boom, "作用域退出失败顶替了原始异常"


@pytest.mark.asyncio
async def test_async_db_update_logs_scope_exit_failure(monkeypatch):
    """
    异步更新：作用域退出失败要留下记录，不能静默吞掉。
    """
    logged = _capture_logger_errors(monkeypatch)
    db = _async_session()
    _install_failing_scope(monkeypatch, db, RuntimeError("exit failed"))

    @decorators_module.async_db_update
    async def _write(db=None):
        """必定失败的异步更新。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        await _write(db=None)

    assert any("exit failed" in str(msg) for msg in logged), \
        f"作用域退出失败被静默吞掉，未留下任何记录：{logged}"


@pytest.mark.asyncio
async def test_async_db_update_scope_exit_failure_does_not_break_success_path(monkeypatch):
    """
    异步更新：业务成功而作用域退出失败时，调用方仍须正常拿到返回值。
    """
    logged = _capture_logger_errors(monkeypatch)
    db = _async_session()
    _install_failing_scope(monkeypatch, db, RuntimeError("exit failed"))

    @decorators_module.async_db_update
    async def _write(db=None):
        """成功的异步更新。"""
        return "written"

    assert await _write(db=None) == "written", "释放失败把成功的写入变成了异常"
    db.commit.assert_awaited_once()
    assert any("exit failed" in str(msg) for msg in logged), \
        f"释放故障既没上抛也没记录，等于彻底消失：{logged}"


def test_db_query_close_failure_does_not_replace_original_error(monkeypatch):
    """
    同步查询：关闭会话失败时，调用方仍须收到原始业务异常。
    """
    db = _sync_session()
    db.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(decorators_module, "ScopedSession", lambda: db)
    boom = _BusinessError("boom")

    @decorators_module.db_query
    def _read(db=None):
        """抛出一个可辨认的异常实例。"""
        raise boom

    with pytest.raises(_BusinessError) as excinfo:
        _read(db=None)

    assert excinfo.value is boom, "关闭会话失败顶替了原始异常"


def test_db_query_logs_close_failure(monkeypatch):
    """
    同步查询：关闭会话失败要留下记录，不能静默吞掉。
    """
    logged = _capture_logger_errors(monkeypatch)
    db = _sync_session()
    db.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(decorators_module, "ScopedSession", lambda: db)

    @decorators_module.db_query
    def _read(db=None):
        """必定失败的查询。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        _read(db=None)

    assert any("close failed" in str(msg) for msg in logged), \
        f"关闭会话失败被静默吞掉，未留下任何记录：{logged}"


def test_db_query_close_failure_does_not_break_success_path(monkeypatch):
    """
    同步查询：查询成功而释放失败时，调用方必须正常拿到查询结果。

    结果已经取到手了，因为归还会话时出的岔子而把它丢掉，是把一次成功的读变成失败。
    """
    logged = _capture_logger_errors(monkeypatch)
    db = _sync_session()
    db.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(decorators_module, "ScopedSession", lambda: db)

    @decorators_module.db_query
    def _read(db=None):
        """成功的查询。"""
        return ["row"]

    assert _read(db=None) == ["row"], "释放失败把成功的查询变成了异常"
    assert any("close failed" in str(msg) for msg in logged), \
        f"释放故障既没上抛也没记录，等于彻底消失：{logged}"


@pytest.mark.asyncio
async def test_async_db_query_scope_exit_failure_does_not_replace_original_error(monkeypatch):
    """
    异步查询：作用域退出失败时，调用方仍须收到原始业务异常。
    """
    db = _async_session()
    _install_failing_scope(monkeypatch, db, RuntimeError("exit failed"))
    boom = _BusinessError("boom")

    @decorators_module.async_db_query
    async def _read(db=None):
        """抛出一个可辨认的异常实例。"""
        raise boom

    with pytest.raises(_BusinessError) as excinfo:
        await _read(db=None)

    assert excinfo.value is boom, "作用域退出失败顶替了原始异常"


@pytest.mark.asyncio
async def test_async_db_query_logs_scope_exit_failure(monkeypatch):
    """
    异步查询：作用域退出失败要留下记录，不能静默吞掉。
    """
    logged = _capture_logger_errors(monkeypatch)
    db = _async_session()
    _install_failing_scope(monkeypatch, db, RuntimeError("exit failed"))

    @decorators_module.async_db_query
    async def _read(db=None):
        """必定失败的异步查询。"""
        raise _BusinessError("boom")

    with pytest.raises(_BusinessError):
        await _read(db=None)

    assert any("exit failed" in str(msg) for msg in logged), \
        f"作用域退出失败被静默吞掉，未留下任何记录：{logged}"


@pytest.mark.asyncio
async def test_async_db_query_scope_exit_failure_does_not_break_success_path(monkeypatch):
    """
    异步查询：查询成功而作用域退出失败时，调用方仍须正常拿到查询结果。
    """
    logged = _capture_logger_errors(monkeypatch)
    db = _async_session()
    _install_failing_scope(monkeypatch, db, RuntimeError("exit failed"))

    @decorators_module.async_db_query
    async def _read(db=None):
        """成功的异步查询。"""
        return ["row"]

    assert await _read(db=None) == ["row"], "释放失败把成功的查询变成了异常"
    assert any("exit failed" in str(msg) for msg in logged), \
        f"释放故障既没上抛也没记录，等于彻底消失：{logged}"
