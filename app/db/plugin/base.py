"""插件专属声明式基类工厂。"""

from __future__ import annotations

from typing import Type

from sqlalchemy.orm import DeclarativeBase

from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


def plugin_declarative_base(
    plugin_id: str, instance_id: str = DEFAULT_INSTANCE_ID
) -> Type[DeclarativeBase]:
    """
    产出插件实例专属的声明式基类，每次调用都返回携带全新 ``MetaData`` 的类。

    插件模型继承返回的基类定义即可落在独立注册表里，不与宿主 ``app.db.base.Base``
    或其它插件的同名表互相冲突。插件热重载时其模块被重新 import，本函数也随之
    重新调用，天然拿到全新的 ``MetaData``，避免在同一 ``MetaData`` 上重复定义
    同名表报错。
    :param plugin_id: 插件标识
    :param instance_id: 插件实例标识，仅用于类名辨识，不参与隔离本身
    :return: 声明式基类
    """

    class _PluginDeclarativeBase(DeclarativeBase):
        pass

    _PluginDeclarativeBase.__name__ = f"PluginBase_{plugin_id}_{instance_id}"
    _PluginDeclarativeBase.__qualname__ = _PluginDeclarativeBase.__name__
    return _PluginDeclarativeBase
