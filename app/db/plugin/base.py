"""插件专属声明式基类工厂。"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase

__all__ = ["plugin_declarative_base"]


def plugin_declarative_base() -> type[DeclarativeBase]:
    """
    产出携带全新 ``MetaData`` 的声明式基类。

    插件模型继承本函数的返回值定义，其表便注册在独立的 ``MetaData`` 上：既不与宿主
    ``app.db.base.Base`` 抢同一份注册表，也允许两个插件各自定义同名表。插件热重载会
    重新执行插件模块、重新调用本函数，因此每次都拿到干净的注册表，不会在同一
    ``MetaData`` 上重复定义同名表而报错。
    :return: 声明式基类
    """

    class PluginBase(DeclarativeBase):  # type: ignore[misc]  # SQLAlchemy 无 py.typed 基类
        """插件自有表的声明式基类。"""

    return PluginBase
