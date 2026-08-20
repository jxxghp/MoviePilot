"""插件服务实例声明的契约校验。

服务实例族的实现类不共享基类——内建下载器、媒体服务器与通知客户端各自独立，
彼此没有共同祖先，因此契约无法按继承判定，只能按「能否被宿主按既定方式构造」
判定：宿主对每条用户配置执行 ``impl(name=配置名, **配置内容)``，校验即确认该
调用形状在签名上成立，且不真正构造实例。
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

from app.runtime.extensions.declaration import (
    SERVICE_INSTANCE_CONFIG_KEYS,
    ServiceInstanceDeclaration,
    declaration_config_component,
    declaration_config_form,
    declaration_impl,
    declaration_service_instance_identity,
)
from app.runtime.extensions.plugin.config_interface import config_interface_violation

# 构造实例时由宿主固定填入的关键字参数名，其余关键字均来自用户配置内容
_INSTANCE_NAME_KEYWORD = "name"


def service_instance_declaration_violation(
    declaration: Any, *, render_mode: Optional[str] = None
) -> Optional[str]:
    """
    校验服务实例声明是否满足登记契约

    契约要求声明是 `ServiceInstanceDeclaration` 实例、配置键属于可声明服务实例的
    服务配置族、类型标识与展示名称非空、实现是抽象方法已全部落地的类，且其构造
    签名能接受 ``impl(name=..., **config)``；配置界面二选一，规则与存储声明相同。
    任一不满足都拒绝登记，不留到构造实例时才失败。

    :param declaration: `ServiceInstanceDeclaration` 实例
    :param render_mode: 声明该服务实例的扩展当前的渲染模式；为 None 时跳过
        ``config_component`` 与渲染模式的一致性校验
    :return: 违反契约的描述；声明合规时为 None
    """
    if not isinstance(declaration, ServiceInstanceDeclaration):
        return f"{declaration!r} 不是 ServiceInstanceDeclaration 实例"
    try:
        impl = declaration_impl(declaration)
        config_key, service_type, name = declaration_service_instance_identity(declaration)
        config_form = declaration_config_form(declaration)
        config_component = declaration_config_component(declaration)
    except Exception as error:
        return f"读取服务实例声明出错：{error}"
    if not config_key:
        return "未声明非空的服务配置键 config_key"
    if config_key not in SERVICE_INSTANCE_CONFIG_KEYS:
        return (
            f"config_key {config_key!r} 不是可声明服务实例的服务配置键，"
            f"可选值为 {list(SERVICE_INSTANCE_CONFIG_KEYS)}"
        )
    if not service_type:
        return "未声明非空的类型标识 type"
    if not name:
        return "未声明非空的展示名称 name"
    if not inspect.isclass(impl):
        return "impl 缺失或不是类"
    unimplemented = getattr(impl, "__abstractmethods__", None)
    if unimplemented:
        return f"{impl!r} 未实现抽象方法：{sorted(unimplemented)}"
    signature_violation = _instantiation_signature_violation(impl)
    if signature_violation:
        return signature_violation
    return config_interface_violation(config_form, config_component, render_mode=render_mode)


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
