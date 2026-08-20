"""插件模块声明的契约校验。

``service_config`` 的合法取值来自 ``SystemConfigKey``，定义在 ``app.schemas``——
该层不属于依赖矩阵禁止的 ``app.db``/``app.agent``，因此本模块可以直接引用，
不需要像存储、智能体工具声明那样绕开反向依赖限制。
"""

from __future__ import annotations

from typing import Any, Optional

from app.runtime.extensions.declaration import declaration_methods
from app.schemas.types import SystemConfigKey

# service_config 的合法取值集合，取自 SystemConfigKey 的全部成员值
_KNOWN_SERVICE_CONFIG_KEYS = frozenset(member.value for member in SystemConfigKey)


def module_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验模块声明是否满足登记契约

    契约要求方法表是非空映射、键均为非空字符串、值均可调用；声明了
    ``service_config`` 时其取值须是 `SystemConfigKey` 的已知成员，不合法的
    取值一律拒绝，包括非字符串类型——空白或未声明该字段则视为不归属任何
    服务族，不受此项约束。四项中任一不满足都拒绝登记，不留到调用时才失败。

    :param declaration: `ModuleDeclaration` 实例，或插件直接交出的方法表字典
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        methods = declaration_methods(declaration)
        service_config = getattr(declaration, "service_config", None)
    except Exception as error:
        return f"读取模块声明出错：{error}"
    if not methods:
        return "methods 缺失或为空映射"
    for name, func in methods.items():
        if not isinstance(name, str) or not name.strip():
            return f"方法名 {name!r} 不是非空字符串"
        if not callable(func):
            return f"方法 {name!r} 对应的实现不可调用"
    if service_config and service_config not in _KNOWN_SERVICE_CONFIG_KEYS:
        return f"service_config {service_config!r} 不是已知的服务配置键"
    return None
