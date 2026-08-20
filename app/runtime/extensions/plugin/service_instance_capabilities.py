"""插件服务实例声明的契约校验。

服务实例族的实现类不共享基类——内建下载器、媒体服务器与通知客户端各自独立，
彼此没有共同祖先，因此契约无法按继承判定，只能按「能否被宿主按声明的方式构造」
判定：``impl`` 路径确认 ``impl(name=配置名, **配置内容)`` 的调用形状在签名上成立，
``factory`` 路径确认工厂可调用且能接受单个位置参数。两条路径都只做签名内省，
不真正构造实例。
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

from app.runtime.extensions.declaration import (
    SERVICE_INSTANCE_CAPABILITIES,
    ServiceInstanceDeclaration,
    declaration_config_component,
    declaration_config_form,
    declaration_service_instance_constructor,
    declaration_service_instance_identity,
)
from app.runtime.extensions.plugin.config_interface import config_interface_violation

# 构造实例时由宿主固定填入的关键字参数名，其余关键字均来自用户配置内容
_INSTANCE_NAME_KEYWORD = "name"

# 内省工厂调用签名时填入的占位实参，签名绑定只匹配形参不读取实参内容
_FACTORY_PROBE_ARGUMENT = object()


def service_instance_declaration_violation(
    declaration: Any, *, render_mode: Optional[str] = None
) -> Optional[str]:
    """
    校验服务实例声明是否满足登记契约

    契约要求声明是 `ServiceInstanceDeclaration` 实例、能力标签属于可声明服务实例的
    服务族、类型标识与展示名称非空、``impl`` 与 ``factory`` 恰好给出其一且该路径的
    调用签名成立；配置界面二选一，规则与存储声明相同。任一不满足都拒绝登记，不留
    到构造实例时才失败。

    :param declaration: `ServiceInstanceDeclaration` 实例
    :param render_mode: 声明该服务实例的扩展当前的渲染模式；为 None 时跳过
        ``config_component`` 与渲染模式的一致性校验
    :return: 违反契约的描述；声明合规时为 None
    """
    if not isinstance(declaration, ServiceInstanceDeclaration):
        return f"{declaration!r} 不是 ServiceInstanceDeclaration 实例"
    try:
        impl, factory = declaration_service_instance_constructor(declaration)
        capability, service_type, name = declaration_service_instance_identity(declaration)
        config_form = declaration_config_form(declaration)
        config_component = declaration_config_component(declaration)
    except Exception as error:
        return f"读取服务实例声明出错：{error}"
    if not capability:
        return "未声明非空的能力标签 capability"
    if capability not in SERVICE_INSTANCE_CAPABILITIES:
        return (
            f"capability {capability!r} 不是可声明服务实例的能力标签，"
            f"可选值为 {list(SERVICE_INSTANCE_CAPABILITIES)}"
        )
    if not service_type:
        return "未声明非空的类型标识 type"
    if not name:
        return "未声明非空的展示名称 name"
    constructor_violation = _constructor_violation(impl, factory)
    if constructor_violation:
        return constructor_violation
    return config_interface_violation(config_form, config_component, render_mode=render_mode)


def _constructor_violation(impl: Any, factory: Any) -> Optional[str]:
    """
    校验声明给出的构造路径是否唯一且成立

    两条路径的语义不同：``impl`` 由宿主按关键字展开用户配置，``factory`` 把整条
    配置原样交给扩展。同时给出无从判断按哪条构造，都不给出则没有构造入口，两者
    均视为声明本身不成立。

    :param impl: 声明携带的实现类
    :param factory: 声明携带的实例工厂
    :return: 违反契约的描述；构造路径唯一且签名成立时为 None
    """
    if impl is not None and factory is not None:
        return "impl 与 factory 同时给出，无法确定构造方式"
    if impl is None and factory is None:
        return "impl 与 factory 均未给出，宿主无从构造实例"
    if factory is not None:
        return _factory_signature_violation(factory)
    if not inspect.isclass(impl):
        return "impl 不是类"
    unimplemented = getattr(impl, "__abstractmethods__", None)
    if unimplemented:
        return f"{impl!r} 未实现抽象方法：{sorted(unimplemented)}"
    return _instantiation_signature_violation(impl)


def _factory_signature_violation(factory: Any) -> Optional[str]:
    """
    校验实例工厂能否被宿主按 ``factory(配置对象)`` 调用

    工厂内部怎么构造实例由扩展自行决定，宿主只关心调用形状，因此校验止于「可调用
    且接受单个位置参数」，不追问返回值类型。

    :param factory: 声明携带的实例工厂
    :return: 违反契约的描述；调用形状成立时为 None
    """
    if not callable(factory):
        return f"{factory!r} 不可调用，无法作为实例工厂"
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError) as error:
        return f"{factory!r} 的调用签名无法内省：{error}"
    try:
        signature.bind(_FACTORY_PROBE_ARGUMENT)
    except TypeError as error:
        return f"{factory!r} 的调用签名不接受单个位置参数：{error}"
    return None


def _instantiation_signature_violation(impl: Any) -> Optional[str]:
    """
    校验实现类的构造签名能否接受 ``impl(name=..., **config)``

    只做签名内省，不构造实例：构造会连上外部服务，而登记发生在插件启动与配置
    生效路径上，不应因外部服务不可达而拒绝声明。

    :param impl: 实现类，调用方已保证是类
    :return: 违反契约的描述；签名可接受该调用形状时为 None
    """
    try:
        signature = inspect.signature(impl)
    except (TypeError, ValueError) as error:
        return f"{impl!r} 的构造签名无法内省：{error}"
    accepts_name = False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            accepts_name = True
            continue
        if parameter.name == _INSTANCE_NAME_KEYWORD and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            accepts_name = True
            continue
        # 仅限位置的必填参数无法由关键字填入，用户配置再全也补不上
        if (
            parameter.kind is inspect.Parameter.POSITIONAL_ONLY
            and parameter.default is inspect.Parameter.empty
        ):
            return f"{impl!r} 的构造签名含仅限位置的必填参数 {parameter.name!r}，无法按关键字构造"
    if not accepts_name:
        return f"{impl!r} 的构造签名不接受关键字参数 {_INSTANCE_NAME_KEYWORD!r}"
    return None
