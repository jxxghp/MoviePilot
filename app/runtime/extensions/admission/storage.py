"""存储族服务实例声明里那部分只有存储才有的契约判定。

存储类型与下载器、媒体服务器、消息通知同走 `ServiceInstanceDeclaration`，只是构造
协议不同：``impl`` 不是被宿主按关键字展开构造的实现类，而是按令牌取用的存储后端类，
因此它的契约是「派生自存储基类且抽象方法已全部落地」，不是「构造签名接受 name」。

存储基类 ``StorageBase`` 定义在 ``app.modules._base.storage``，而本模块所在的
``app.runtime`` 层依赖矩阵禁止反向引用 ``app.modules``；子类判定改走 MRO 上的
模块与限定名比对，不依赖 import。
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

# 存储基类的模块与限定名，用于在不引入反向依赖的前提下判定实现类的真实继承关系
_STORAGE_BASE_QUALIFIED_NAME = "app.modules._base.storage.StorageBase"


def implements_storage_base(impl: Any) -> bool:
    """
    判断实现类是否派生自存储基类

    :param impl: 待判定的实现类
    :return: MRO 中存在与存储基类同源的类时为 True
    """
    for klass in getattr(impl, "__mro__", ()):
        if f"{klass.__module__}.{klass.__qualname__}" == _STORAGE_BASE_QUALIFIED_NAME:
            return True
    return False


def storage_backend_violation(impl: Any) -> Optional[str]:
    """
    校验存储类型声明的后端类是否满足登记契约

    契约要求实现是类、派生自存储基类 ``StorageBase``、其抽象方法已全部落地。不校验
    构造签名：存储后端不由宿主按关键字展开配置构造，配置由后端自己按存储令牌懒读，
    构造走工厂——宿主默认那一个或声明自带的那一个。

    :param impl: 声明携带的存储后端类
    :return: 违反契约的描述；后端合规时为 None
    """
    if not inspect.isclass(impl):
        return "impl 缺失或不是类"
    if not implements_storage_base(impl):
        return f"{impl!r} 不是存储基类 StorageBase 的子类"
    unimplemented = getattr(impl, "__abstractmethods__", None)
    if unimplemented:
        return f"{impl!r} 未实现抽象方法：{sorted(unimplemented)}"
    return None
