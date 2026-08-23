"""
数据库事务装饰器。

同步/异步各一对：查询装饰器负责会话的获取与释放，更新装饰器额外负责提交与回滚。
未显式传入会话时自动创建，并在结束时归还——异步路径经 async_session_scope 收口，
连接池与配额都在那里生效。

收尾故障（rollback / close / __aexit__ 自身抛异常）一律只记日志、不上抛。理由与代价
都要写明，别当成漏写的 raise：

- 连接断开、事务已失效这类故障恰恰最容易发生在「出错之后」的收尾阶段。裸写收尾语句时
  它一抛错就顶替掉原始异常，调用方看到的只剩「connection reset」，业务异常连类型都被
  换掉，按类型分流的 except（唯一约束冲突要重试、参数错误要报错）一并失配。
- 代价是成功路径的行为随之改变：func() 成功、close() 失败时，调用方**静默拿到返回值**，
  故障只进日志。这是有意为之——close() 失败时事务已经提交、业务确实成功了，且
  SQLAlchemy 归还连接时已在池层吞掉异常并 invalidate 坏连接，再把释放故障升级成调用方
  的异常，只会让一次已经落库的写入看起来像失败，诱发重复提交。
"""
from typing import Any, Awaitable, Callable, Optional, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.session import ScopedSession, async_session_scope
from app.runtime.log import logger

_R = TypeVar("_R")

# 正式装饰器会重写实参列表：未传会话时自行创建一个并塞回 db 位置。因此包装后的可调用
# 对象接受的实参与被包装函数的签名并不一致——用 Callable[..., _R] 如实表达「参数由装饰器
# 接管、返回值原样透传」。否则调用方传 None 或传异步会话都会被判成类型不符，而这恰恰是
# 装饰器存在的理由（各 Oper 的 self._db 常态就是 None）。


def _get_args_db(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Optional[Session]:
    """
    从参数中获取数据库Session对象
    """
    db = None
    if args:
        for arg in args:
            if isinstance(arg, Session):
                db = arg
                break
    if kwargs:
        for key, value in kwargs.items():
            if isinstance(value, Session):
                db = value
                break
    return db


def _get_args_async_db(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Optional[AsyncSession]:
    """
    从参数中获取异步数据库AsyncSession对象
    """
    db = None
    if args:
        for arg in args:
            if isinstance(arg, AsyncSession):
                db = arg
                break
    if kwargs:
        for key, value in kwargs.items():
            if isinstance(value, AsyncSession):
                db = value
                break
    return db


def _update_args_db(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    db: Session,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """
    更新参数中的数据库Session对象，关键字传参时更新db的值，否则更新第1或第2个参数
    """
    if kwargs and 'db' in kwargs:
        kwargs['db'] = db
    elif args:
        if args[0] is None:
            args = (db, *args[1:])
        else:
            args = (args[0], db, *args[2:])
    return args, kwargs


def _update_args_async_db(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    db: AsyncSession,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """
    更新参数中的异步数据库AsyncSession对象，关键字传参时更新db的值，否则更新第1或第2个参数
    """
    if kwargs and 'db' in kwargs:
        kwargs['db'] = db
    elif args:
        if args[0] is None:
            args = (db, *args[1:])
        else:
            args = (args[0], db, *args[2:])
    return args, kwargs


def db_update(func: Callable[..., _R]) -> Callable[..., _R]:
    """
    数据库更新类操作装饰器，第一个参数必须是数据库会话或存在db参数
    """

    def wrapper(*args: Any, **kwargs: Any) -> _R:
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取数据库会话
        db = _get_args_db(args, kwargs)
        if not db:
            # 如果没有获取到数据库会话，创建一个
            db = ScopedSession()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的数据库会话
            args, kwargs = _update_args_db(args, kwargs, db)
        try:
            # 执行函数
            result = func(*args, **kwargs)
            # 提交事务
            db.commit()
        except Exception as err:
            # 回滚事务。回滚自身失败不得顶替原始异常：连接断开、事务已失效这类收尾故障
            # 恰恰最容易发生在「出错之后」，裸写 db.rollback() 时它一抛错，调用方看到的
            # 就只剩「connection reset」，真正的业务异常连类型都被换掉、按类型分流的
            # except 一并失配。故障本身另行记录，不静默吞掉
            try:
                db.rollback()
            except Exception as rollback_err:  # noqa: BLE001  回滚失败不能掩盖原始异常
                logger.error(f"事务回滚失败，原始异常将原样上抛：{rollback_err}")
            raise err
        finally:
            # 关闭数据库会话。释放失败只记录：既不顶替上面正在传播的业务异常，
            # 成功路径下也不把一次已提交的写入变成调用方眼里的失败（见模块说明）
            if _close_db:
                try:
                    db.close()
                except Exception as close_err:  # noqa: BLE001  释放故障不得改变调用结果
                    logger.error(f"释放数据库会话失败：{close_err}")
        return result

    return wrapper


def async_db_update(func: Callable[..., Awaitable[_R]]) -> Callable[..., Awaitable[_R]]:
    """
    异步数据库更新类操作装饰器，第一个参数必须是异步数据库会话或存在db参数
    """

    async def wrapper(*args: Any, **kwargs: Any) -> _R:
        # 是否关闭数据库会话；作用域与 _scope 同生共死，先置空以便静态检查看清
        _close_db = False
        _scope = None
        # 从参数中获取异步数据库会话
        db = _get_args_async_db(args, kwargs)
        if not db:
            # 如果没有获取到异步数据库会话，创建一个。经 async_session_scope
            # 统一收口：常驻主循环走连接池，其余循环走 NullPool 并占用全局配额
            _scope = async_session_scope()
            db = await _scope.__aenter__()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的异步数据库会话
            args, kwargs = _update_args_async_db(args, kwargs, db)
        try:
            # 执行函数
            result = await func(*args, **kwargs)
            # 提交事务
            await db.commit()
        except Exception as err:
            # 回滚事务；与同步路径同理，回滚失败只记录，不顶替原始异常
            try:
                await db.rollback()
            except Exception as rollback_err:  # noqa: BLE001  回滚失败不能掩盖原始异常
                logger.error(f"事务回滚失败，原始异常将原样上抛：{rollback_err}")
            raise err
        finally:
            # 关闭数据库会话
            if _close_db and _scope is not None:
                # 退出会话上下文而不是只 close：配额的释放绑定在 __aexit__ 上，
                # 只关会话会让回退路径的全局配额永不归还，最终把自己饿死。
                # 退出失败同样只记录，不改变调用结果（见模块说明）
                try:
                    await _scope.__aexit__(None, None, None)
                except Exception as close_err:  # noqa: BLE001  释放故障不得改变调用结果
                    logger.error(f"释放数据库会话失败：{close_err}")
        return result

    return wrapper


def db_query(func: Callable[..., _R]) -> Callable[..., _R]:
    """
    数据库查询操作装饰器，第一个参数必须是数据库会话或存在db参数
    注意：db.query列表数据时，需要转换为list返回
    """

    def wrapper(*args: Any, **kwargs: Any) -> _R:
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取数据库会话
        db = _get_args_db(args, kwargs)
        if not db:
            # 如果没有获取到数据库会话，创建一个
            db = ScopedSession()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的数据库会话
            args, kwargs = _update_args_db(args, kwargs, db)
        try:
            # 执行函数
            result = func(*args, **kwargs)
        except Exception as err:
            raise err
        finally:
            # 关闭数据库会话。释放失败只记录，不顶替业务异常、也不影响成功路径的返回值
            # （见模块说明）
            if _close_db:
                try:
                    db.close()
                except Exception as close_err:  # noqa: BLE001  释放故障不得改变调用结果
                    logger.error(f"释放数据库会话失败：{close_err}")
        return result

    return wrapper


def async_db_query(func: Callable[..., Awaitable[_R]]) -> Callable[..., Awaitable[_R]]:
    """
    异步数据库查询操作装饰器，第一个参数必须是异步数据库会话或存在db参数
    注意：db.query列表数据时，需要转换为list返回
    """

    async def wrapper(*args: Any, **kwargs: Any) -> _R:
        # 是否关闭数据库会话
        _close_db = False
        _scope = None
        # 从参数中获取异步数据库会话
        db = _get_args_async_db(args, kwargs)
        if not db:
            # 如果没有获取到异步数据库会话，创建一个。经 async_session_scope
            # 统一收口：常驻主循环走连接池，其余循环走 NullPool 并占用全局配额
            _scope = async_session_scope()
            db = await _scope.__aenter__()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的异步数据库会话
            args, kwargs = _update_args_async_db(args, kwargs, db)
        try:
            # 执行函数
            result = await func(*args, **kwargs)
        except Exception as err:
            raise err
        finally:
            # 关闭数据库会话
            if _close_db and _scope is not None:
                # 退出会话上下文而不是只 close：配额的释放绑定在 __aexit__ 上，
                # 只关会话会让回退路径的全局配额永不归还，最终把自己饿死。
                # 退出失败同样只记录，不改变调用结果（见模块说明）
                try:
                    await _scope.__aexit__(None, None, None)
                except Exception as close_err:  # noqa: BLE001  释放故障不得改变调用结果
                    logger.error(f"释放数据库会话失败：{close_err}")
        return result

    return wrapper
