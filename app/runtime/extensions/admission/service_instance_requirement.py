"""扩展点作用于哪台服务实例：声明的形状判定、候选列举与调用目标裁决。

动作与仪表盘挂在插件的**分身**上，而用户配了多份的是插件提供的**服务实例**，两个
「多」不在同一根轴上。`ServiceInstanceRequirement` 把后一根轴写进声明，宿主据此接手
三件原本由每个插件作者各写一遍的事：渲染实例选择器、校验用户选中的实例仍然存在、
以及在它消失时给出可辨的成因。

**声明期只判形状，不判该族有没有登记。** 判据在取用时机：动作与仪表盘的声明校验不是
装载期跑一次，而是每次投影都重跑（见 `projection/plugin.py` 的 ``provided_actions``
与 ``provided_dashboards``）。把「族已登记」写进声明是否合规的判定，同一条声明就会
因为提问时刻不同而一会儿合规一会儿不合规——某个带进新族的扩展晚一步登记，先前的
拒绝理由自己就消失了。声明是否成立必须只取决于声明自身写了什么。

因此形状判定不比服务族登记表本身更严：`ServiceFamilyRegistry.register` 只要求能力
标签是非空字符串，此处要求同一件事。比登记表严会拒掉一个确实登记得上的族，那正是
「把合法声明挡在门外」。

存在性改由取用期回答，且分开四种成因而不是笼统答一个「不可用」——族没登记、实例被
删了、实例还在但停用了、实例在但类型不在声明收窄的范围里，四者的处置动作各不相同。

未选实例时的裁决按 docs/plugin-extension-architecture.md §7.2：有显式默认调用目标则
用它，没有默认或默认已停用则报错并列出候选，绝不取第一个。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Tuple

from app.runtime.extensions.contract.instance import describe_instance_candidates
from app.runtime.extensions.service_config import (
    service_capability_configs,
    service_instance_default,
    service_instance_enabled,
    service_supports_default_target,
)
from app.runtime.extensions.registry.service_family import service_family_registry

# 选中的实例名在调用两侧共用的参数名：工作流节点按它存用户的选择，宿主解析后按它把
# 实例名交回实现。两侧同名才使「用户选的」与「实现拿到的」不可能是两个东西。
SERVICE_INSTANCE_PARAM = "service_instance"

# 声明的能力标签当前不是已登记的服务族
REQUIREMENT_FAMILY_ABSENT = "family_absent"

# 该族下已没有这个实例名的配置
REQUIREMENT_INSTANCE_ABSENT = "instance_absent"

# 实例配置还在，但已被停用
REQUIREMENT_INSTANCE_DISABLED = "instance_disabled"

# 实例配置还在且已启用，但类型不在声明收窄的类型里
REQUIREMENT_TYPE_EXCLUDED = "type_excluded"


@dataclass(frozen=True, slots=True)
class ServiceInstanceCandidate:
    """一条可供选择的服务实例配置。

    只带身份与启用态，不带 ``config``：候选列表是给用户挑选用的，而配置载荷里装着
    token 与密码，随选择器下发即等于把凭据摊给每一个能编辑工作流的人。

    :param type: 类型标识
    :param name: 实例名
    :param enabled: 该实例是否已启用
    :param is_default_target: 该实例是否为本族的默认调用目标
    """

    type: str
    name: str
    enabled: bool
    is_default_target: bool


def _requirement_field(requirement: Any, field: str) -> Any:
    """读取作用对象声明上的字段原始值，兼容属性对象与映射两种载体。

    :param requirement: 作用对象声明，或与之同形的映射
    :param field: 字段名
    :return: 字段原始值；字段缺失时为 None
    """
    if isinstance(requirement, Mapping):
        return requirement.get(field)
    return getattr(requirement, field, None)


def requirement_capability(requirement: Any) -> Optional[str]:
    """读取作用对象声明的能力标签。

    :param requirement: 作用对象声明，或与之同形的映射
    :return: 去除首尾空白后的能力标签；声明缺失、字段非字符串或为空白时为 None
    """
    if requirement is None:
        return None
    value = _requirement_field(requirement, "capability")
    return value.strip() if isinstance(value, str) and value.strip() else None


def requirement_types(requirement: Any) -> Tuple[str, ...]:
    """读取作用对象声明收窄到的类型标识。

    :param requirement: 作用对象声明，或与之同形的映射
    :return: 类型标识元组，未收窄时为空元组
    """
    if requirement is None:
        return ()
    value = _requirement_field(requirement, "types")
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def service_instance_requirement_violation(requirement: Any) -> Optional[str]:
    """校验作用对象声明的形状。

    只判形状：能力标签是非空字符串、``types`` 是字符串序列且每项非空。不判该族有没有
    登记，判据见本模块文档。

    :param requirement: 作用对象声明；为 None 表示该扩展点与服务实例无关
    :return: 违反契约的描述；声明合规或未声明时为 None
    """
    if requirement is None:
        return None
    if isinstance(requirement, (str, bytes)) or not (
        isinstance(requirement, Mapping) or hasattr(requirement, "capability")
    ):
        return (
            "requires_service_instance 必须是 ServiceInstanceRequirement "
            f"或与之同形的映射，实际是 {type(requirement).__name__}"
        )
    capability = _requirement_field(requirement, "capability")
    if not isinstance(capability, str) or not capability.strip():
        return "requires_service_instance 未声明非空的能力标签 capability"
    types = _requirement_field(requirement, "types")
    if types is None or isinstance(types, (list, tuple)):
        declared = tuple(types or ())
    else:
        return f"requires_service_instance 的 types 必须是序列，实际是 {type(types).__name__}"
    for item in declared:
        if not isinstance(item, str) or not item.strip():
            return f"requires_service_instance 的 types 含非法类型标识：{item!r}"
    return None


def projected_service_instance_requirement(requirement: Any) -> Optional[dict]:
    """把作用对象声明投影为可整体序列化的纯数据。

    投影结果随动作描述与仪表盘元信息下发给前端，因此只保留声明数据本身：能力标签
    与收窄的类型标识过得了进程边界，异语言宿主拿到同一份报文即可渲染同一个选择器。

    :param requirement: 作用对象声明
    :return: 含 capability 与 types 的字典；未声明或能力标签取不到时为 None
    """
    capability = requirement_capability(requirement)
    if not capability:
        return None
    return {"capability": capability, "types": list(requirement_types(requirement))}


def _family_configs(capability: str) -> list:
    """取某族当前全部通过结构校验的实例配置。

    :param capability: 能力标签
    :return: 配置列表；该族没有配置落点时为空列表
    """
    return service_capability_configs(capability) or []


def service_instance_candidates(requirement: Any) -> Tuple[ServiceInstanceCandidate, ...]:
    """列出满足作用对象声明的全部候选实例。

    停用的实例照样列出并标注启用态：用户看到「配了但停用了」才知道该去启用哪一条，
    而把它整个藏起来只会让人以为配置丢了。排序按「类型标识、实例名」升序而不按写入
    先后——选择器与报错文案都要按稳定顺序呈现，写入先后用户既看不见也无法预期。

    :param requirement: 作用对象声明
    :return: 候选实例元组，按类型标识与实例名升序；能力标签取不到或该族无配置时为空元组
    """
    capability = requirement_capability(requirement)
    if not capability:
        return ()
    narrowed = set(requirement_types(requirement))
    candidates = [
        ServiceInstanceCandidate(
            type=conf.type,
            name=conf.name,
            enabled=service_instance_enabled(capability, conf),
            is_default_target=service_instance_default(conf),
        )
        for conf in _family_configs(capability)
        if getattr(conf, "name", None) and getattr(conf, "type", None)
        and (not narrowed or conf.type in narrowed)
    ]
    return tuple(sorted(candidates, key=lambda item: (item.type, item.name)))


def describe_candidates(candidates: Iterable[ServiceInstanceCandidate]) -> str:
    """把候选实例列表整理成报错文案里的候选描述。

    :param candidates: 候选实例集合
    :return: 形如 ``主力（已启用）、备用（已停用）`` 的描述，一个候选都没有时为「无」
    """
    return describe_instance_candidates(
        (item.name, item.enabled) for item in candidates
    )


def service_instance_reference_issue(requirement: Any, name: Optional[str]) -> Optional[str]:
    """判定一次实例引用当前还成不成立，并分开四种成因。

    成因分开而不是笼统答一个「不可用」：装回或重建配置、启用配置、以及改选一台类型
    对得上的实例，三种处置动作完全不同。文案不在此处产出，只交稳定的成因代码，措辞
    由呈现方按当前语言渲染。

    :param requirement: 作用对象声明
    :param name: 用户选中的实例名；为空表示尚未选择，此时只判族是否登记
    :return: 成因代码；引用成立或未声明作用对象时为 None
    """
    capability = requirement_capability(requirement)
    if not capability:
        return None
    if not service_family_registry.is_registered(capability):
        return REQUIREMENT_FAMILY_ABSENT
    if not name:
        return None
    narrowed = set(requirement_types(requirement))
    matched = [
        conf for conf in _family_configs(capability)
        if getattr(conf, "name", None) == name
    ]
    if not matched:
        return REQUIREMENT_INSTANCE_ABSENT
    if narrowed and not any(conf.type in narrowed for conf in matched):
        return REQUIREMENT_TYPE_EXCLUDED
    in_scope = [conf for conf in matched if not narrowed or conf.type in narrowed]
    if not any(service_instance_enabled(capability, conf) for conf in in_scope):
        return REQUIREMENT_INSTANCE_DISABLED
    return None


def resolve_required_service_instance(requirement: Any, name: Optional[str] = None) -> Optional[str]:
    """确定本次调用应当作用于哪台服务实例。

    未指定实例时按 §7.2 裁决：有已启用的显式默认调用目标则用它，没有默认、默认已停用、
    或默认被 ``types`` 收窄排除在外，一律报错并列出候选，绝不取第一个。没有默认调用目标
    这个概念的族（登录认证）同样报错——那一族里目标永远是用户点出来的具体某个。

    :param requirement: 作用对象声明；为 None 表示该扩展点与服务实例无关
    :param name: 调用方指定的实例名，为空时按默认调用目标裁决
    :return: 本次调用应当作用的实例名；未声明作用对象时为 None
    :raises LookupError: 指定的实例已不可用，或未指定且裁决不出唯一的默认调用目标
    """
    capability = requirement_capability(requirement)
    if not capability:
        return None
    if name:
        issue = service_instance_reference_issue(requirement, name)
        if issue is None:
            return name
        raise LookupError(_issue_message(
            capability, name, issue,
            describe_candidates(service_instance_candidates(requirement)),
        ))
    if not service_family_registry.is_registered(capability):
        raise LookupError(f"服务能力 {capability} 不是已登记的服务族，无法确定作用的实例")
    candidates = service_instance_candidates(requirement)
    described = describe_candidates(candidates)
    if not service_supports_default_target(capability):
        raise LookupError(
            f"服务能力 {capability} 没有默认调用目标，调用必须显式指定实例；"
            f"可选实例：{described}"
        )
    default_target = next(
        (item for item in candidates if item.is_default_target), None
    )
    if default_target is not None and default_target.enabled:
        return default_target.name
    if default_target is not None:
        raise LookupError(
            f"服务能力 {capability} 的默认调用目标 {default_target.name} 已停用，"
            f"调用必须显式指定实例；可选实例：{described}"
        )
    raise LookupError(
        f"服务能力 {capability} 未设置默认调用目标，调用必须显式指定实例；"
        f"可选实例：{described}"
    )


def accepts_keyword(impl: Any, keyword: str) -> bool:
    """判断可调用对象能否接收指定的关键字实参。

    宿主据此决定要不要把解析出的实例名交给实现：声明了作用对象却把参数写窄了的实现
    仍按原形状调用，退回到该字段存在之前的行为，而不是当场抛 TypeError。

    :param impl: 待判定的可调用对象
    :param keyword: 关键字名
    :return: 实现显式声明了该关键字或接受 ``**kwargs`` 时为 True；签名无法内省时为 False
    """
    try:
        signature = inspect.signature(impl)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == keyword and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _issue_message(capability: str, name: str, issue: str, candidates: str) -> str:
    """把引用失效的成因代码渲染成运行期报错文案。

    每种成因各拼成一句完整的话而不是「成因片段加候选片段」：本地化按整句登记模式，
    拼出来的句子在任何一份译文里都找不到对应项。

    :param capability: 能力标签
    :param name: 用户选中的实例名
    :param issue: 成因代码
    :param candidates: 候选实例描述
    :return: 报错文案
    """
    if issue == REQUIREMENT_FAMILY_ABSENT:
        return f"服务能力 {capability} 不是已登记的服务族，无法确定作用的实例"
    if issue == REQUIREMENT_INSTANCE_ABSENT:
        return (
            f"服务能力 {capability} 下已不存在名为 {name} 的实例配置，"
            f"请改选一个仍然存在的实例；可选实例：{candidates}"
        )
    if issue == REQUIREMENT_INSTANCE_DISABLED:
        return (
            f"服务能力 {capability} 下名为 {name} 的实例配置已停用，"
            f"请启用它或改选一个已启用的实例；可选实例：{candidates}"
        )
    return (
        f"服务能力 {capability} 下名为 {name} 的实例配置类型不在本扩展点声明的范围内，"
        f"请改选一个类型对得上的实例；可选实例：{candidates}"
    )
