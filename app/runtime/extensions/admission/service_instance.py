"""插件服务实例声明的契约校验。

服务实例族的实现类不共享基类——内建下载器、媒体服务器与通知客户端各自独立，
彼此没有共同祖先，因此契约无法按继承判定，只能按「能否被宿主按声明的方式构造」
判定：``impl`` 路径确认 ``impl(name=配置名, **配置内容)`` 的调用形状在签名上成立，
``factory`` 路径确认工厂可调用且能接受单个位置参数。两条路径都只做签名内省，
不真正构造实例。

构造形状之外还判一层实例形状：宿主把实例交出去之后，族级取用链上有几个无保护的
直调，缺席即在背景回路里抛异常。这几个方法名收在
`app.runtime.extensions.contract.service_instance`，未登记必填集的族与
``factory`` 路径都不判。

存储族的构造协议另有一套，判定随之换一条：``impl`` 是按令牌取用的存储后端类，
契约按继承判定（见 `app.runtime.extensions.admission.storage`），构造一律
走工厂——不给 ``factory`` 时用宿主默认工厂，因此该族的 ``factory`` 是可选项而不是
``impl`` 的替代项。

声明携带的配置契约在此一并判定：契约本身要落在受支持的子集内，否则宿主评估不了
它，配置写入与实例构造两处的判定都会失去依据。
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

from app.runtime.deprecation.policy import is_active as deprecation_is_active
from app.runtime.extensions.contract.config_schema import config_schema_violation
from app.runtime.extensions.contract.declaration import (
    ServiceInstanceDeclaration,
    declaration_config_component,
    declaration_config_form,
    declaration_config_schema,
    declaration_service_instance_constructor,
    declaration_service_instance_identity,
    declaration_service_instance_multi_instance,
)
from app.runtime.extensions.admission.config_interface import config_interface_violation
from app.runtime.extensions.contract.service_instance import (
    service_instance_shape_violation,
)
from app.runtime.extensions.admission.storage import storage_backend_violation
from app.runtime.extensions.service_config import STORAGE_CAPABILITY
from app.runtime.extensions.registry.service_family import service_family_registry

# 构造实例时由宿主固定填入的关键字参数名，其余关键字均来自用户配置内容
_INSTANCE_NAME_KEYWORD = "name"

# 「服务实例类型声明不带配置契约」的废弃标识，阶段推进即把契约从可选变为必填
SERVICE_INSTANCE_SCHEMA_DEPRECATION = "plugin.service_instance_without_config_schema"

# 内省工厂调用签名时填入的占位实参，签名绑定只匹配形参不读取实参内容
_FACTORY_PROBE_ARGUMENT = object()


def service_instance_declaration_violation(
    declaration: Any, *, render_mode: Optional[str] = None
) -> Optional[str]:
    """
    校验服务实例声明是否满足登记契约

    契约要求声明是 `ServiceInstanceDeclaration` 实例、能力标签是服务族登记表中已登记
    的族、类型标识与展示名称非空、``multi_instance`` 是布尔值、构造路径成立；配置界面
    二选一。任一不满足都拒绝登记，不留到构造实例时才失败。

    构造路径按族判定：除存储外的各族要求 ``impl`` 与 ``factory`` 恰好给出其一且该路径的
    调用签名成立；存储族要求 ``impl`` 是合契约的存储后端类，``factory`` 可给可不给——不给
    即用宿主默认工厂。

    构造路径成立之后再判实例形状：``impl`` 须带齐本族取用链上无保护直调的方法，必填集
    之外的方法缺席即视为不提供该能力，不必写空桩。

    ``config_schema`` 声明了就必须落在受支持的子集内，声明一份宿主评估不了的契约与
    不声明是两回事，后者只是没有契约，前者是一份看起来有效、实际拦不住任何东西的
    契约。是否**必须**声明由废弃阶段决定：当前阶段照常接受未声明契约的声明，阶段
    推进到默认关闭后同一处即判为违约，无需改动本函数。

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
        multi_instance = declaration_service_instance_multi_instance(declaration)
        config_form = declaration_config_form(declaration)
        config_component = declaration_config_component(declaration)
        config_schema = declaration_config_schema(declaration)
    except Exception as error:
        return f"读取服务实例声明出错：{error}"
    if not capability:
        return "未声明非空的能力标签 capability"
    if not service_family_registry.is_registered(capability):
        return (
            f"capability {capability!r} 不是可声明服务实例的能力标签，"
            f"可选值为 {list(service_family_registry.capabilities())}"
        )
    if not service_type:
        return "未声明非空的类型标识 type"
    if not name:
        return "未声明非空的展示名称 name"
    if not isinstance(multi_instance, bool):
        return f"multi_instance {multi_instance!r} 不是布尔值，无法判定该类型能配几份"
    constructor_violation = _constructor_violation(capability, impl, factory)
    if constructor_violation:
        return constructor_violation
    shape_violation = service_instance_shape_violation(capability, impl)
    if shape_violation:
        return shape_violation
    schema_violation = _config_schema_violation(capability, config_schema, impl)
    if schema_violation:
        return schema_violation
    return config_interface_violation(config_form, config_component, render_mode=render_mode)


def _config_schema_violation(
    capability: str, config_schema: Any, impl: Any
) -> Optional[str]:
    """
    校验声明携带的配置契约

    ``impl`` 路径下宿主按 ``impl(name=配置名, **配置内容)`` 构造，实例名由宿主填入，
    契约再声明同名字段会让构造得到两个 ``name`` 关键字；``factory`` 路径整条配置原样
    交给扩展，不存在这次填入，因此该保留字只在 ``impl`` 路径下成立。存储族的构造一律
    走工厂，配置不经关键字展开，同样没有保留字段名。

    :param capability: 声明的能力标签
    :param config_schema: 声明携带的配置契约原始值
    :param impl: 声明携带的实现类，为 None 表示走 factory 路径
    :return: 违反契约的描述；契约合规时为 None
    """
    if config_schema is None and not deprecation_is_active(
        SERVICE_INSTANCE_SCHEMA_DEPRECATION
    ):
        return "未声明配置契约 config_schema，宿主无从判定该类型的配置形状"
    expands_config = impl is not None and capability != STORAGE_CAPABILITY
    reserved = (_INSTANCE_NAME_KEYWORD,) if expands_config else ()
    return config_schema_violation(config_schema, reserved_property_names=reserved)


def _constructor_violation(capability: str, impl: Any, factory: Any) -> Optional[str]:
    """
    校验声明给出的构造路径是否成立

    通用的两条路径语义不同：``impl`` 由宿主按关键字展开用户配置，``factory`` 把整条
    配置原样交给扩展。同时给出无从判断按哪条构造，都不给出则没有构造入口，两者均视为
    声明本身不成立。

    存储族不适用这条互斥：``impl`` 在该族里回答的是「后端类是谁」而不是「怎么构造」，
    按令牌取用的登记要用它，因此它必填；构造一律走工厂，``factory`` 缺省即宿主默认
    那一个。

    :param capability: 声明的能力标签
    :param impl: 声明携带的实现类
    :param factory: 声明携带的实例工厂
    :return: 违反契约的描述；构造路径成立时为 None
    """
    if capability == STORAGE_CAPABILITY:
        return _storage_constructor_violation(impl, factory)
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


def _storage_constructor_violation(impl: Any, factory: Any) -> Optional[str]:
    """
    校验存储类型声明的后端类与可选工厂

    :param impl: 声明携带的存储后端类
    :param factory: 声明携带的实例工厂，为 None 表示用宿主默认工厂
    :return: 违反契约的描述；构造路径成立时为 None
    """
    backend_violation = storage_backend_violation(impl)
    if backend_violation:
        return backend_violation
    return _factory_signature_violation(factory) if factory is not None else None


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
