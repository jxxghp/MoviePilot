"""``app.db`` 对外契约的边界。

``__all__`` 是这个包唯一一份机器可读的对外承诺，而当前的写法**看起来像个疏漏**：
``SessionFactory`` / ``AsyncSessionFactory`` / ``ScopedSession`` 三个名字在
``app/db/__init__.py`` 里明明 import 了，却不出现在 ``__all__`` 中。下一个读到这段代码
的人很容易顺手把它们补回去——那会在无人察觉的情况下把契约重新放宽。

这三个名字建出来的是**绕过事务装饰器**的裸会话：不提交、不回滚、不释放，全靠调用方
自己兜底。它们之所以还留在模块里，只是因为包内的 scheduler、postgresql 模块与 Alembic
迁移脚本用直接导入的方式在用（直接导入不受 ``__all__`` 约束）。

所以这里同时钉两头：契约里没有它们，但包内的既有导入不能被这个决定误伤。
"""
import app.db as db_package


# 降级为内部实现细节的三个会话工厂
INTERNAL_FACTORY_NAMES = ("SessionFactory", "AsyncSessionFactory", "ScopedSession")


def test_session_factories_are_not_part_of_the_public_contract():
    """
    三个会话工厂不得出现在 ``__all__`` 里。

    插件访问宿主数据应走 ``DbOper``；插件自有表可使用 ``db_query`` / ``async_db_query``
    装饰器，由装饰器收口插件自有会话的提交、回滚与释放。
    """
    leaked = [name for name in INTERNAL_FACTORY_NAMES if name in db_package.__all__]
    assert not leaked, f"会话工厂被重新放进了对外契约：{leaked}"


def test_session_factories_remain_importable_for_in_repo_callers():
    """
    契约收窄不等于删除：包内既有的直接导入必须照常可用。

    ``app/scheduler.py``、``app/modules/postgresql/__init__.py`` 与
    ``database/versions/*.py`` 都在用 ``from app.db import SessionFactory``，
    这类直接导入本就不受 ``__all__`` 影响，此处显式钉住以免连带删除。
    """
    for name in INTERNAL_FACTORY_NAMES:
        assert callable(getattr(db_package, name, None)), f"{name} 不再可从 app.db 导入"


def test_engines_stay_in_the_public_contract():
    """
    ``Engine`` / ``AsyncEngine`` 留在契约内。

    建表、Alembic 迁移与连接诊断确实需要引擎**对象**本身，事务装饰器覆盖不到这些用途，
    仓库外拿它是正当的。

    只断言名字在不在 ``__all__``，不去真的取它：这两个名字由模块级 ``__getattr__`` 解析，
    取属性即创建引擎，而本用例并不想在测试进程里凭空建一个没人释放的异步引擎。
    """
    for name in ("Engine", "AsyncEngine"):
        assert name in db_package.__all__, f"{name} 被移出了对外契约"


def test_documented_plugin_entrypoints_are_exported():
    """
    契约里必须留有插件真正该走的那条路：``DbOper`` 基类与四个事务装饰器。

    否则「工厂降级为内部细节」就成了一句没有出口的话。
    """
    for name in ("DbOper", "db_query", "db_update", "async_db_query", "async_db_update"):
        assert name in db_package.__all__, f"插件数据访问入口 {name} 不在 __all__ 中"
