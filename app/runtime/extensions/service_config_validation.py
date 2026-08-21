"""服务实例配置写入前的契约判定与整形。

写入路径与实例构造路径判定的是同一件事——一条服务配置的内容合不合它那个类型声明的
契约——但站位不同：写入路径能在配置落盘前把错误退回给用户并说明原因，构造路径只能
在配置已经存下去之后跳过这一条。两者共用同一个判定函数，因此不会出现「写得进去、
用不起来」的分歧。

整形与判定同处一处：两者都要回答「这一条属于哪个类型、哪些字段归类型、哪些字段归
宿主」，分开放会让同一份判断存在两处。判定与整形都同时依赖配置读取端口与服务实例
登记表；后者依赖前者，故这层接线不能收回任一侧，只能另立一处。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from app.runtime.extensions.config_schema import config_value_violations
from app.runtime.extensions.service_config import service_host_fields
from app.runtime.extensions.service_family_registry import service_family_registry
from app.runtime.extensions.service_instance_registry import service_instance_registry


def service_config_write_violation(capability: Optional[str], value: Any) -> Optional[str]:
    """
    判定一次整族服务配置写入是否合各条目所属类型声明的契约

    整条写入要么全进要么全退：写入端点收到的是整份配置列表，只挑掉不合契约的那几条
    等于替用户丢数据，而用户既看不到丢了什么也无从恢复。因此一条不合契约即退回整次
    写入，并逐条说明是哪个实例的哪个字段有问题。

    不属于任何服务族的能力标签、以及未登记或未声明契约的类型都不产生判定，行为与本
    判定加入之前完全一致。

    :param capability: 该族的能力标签，不属于服务实例族时不产生判定
    :param value: 待写入的整族配置值
    :return: 面向用户的拒绝原因；可以写入时为 None
    """
    if not capability:
        return None
    if value is None:
        return None
    if not isinstance(value, list):
        return f"{_family_label(capability)}配置应为列表，实际为 {type(value).__name__}"
    reasons: List[str] = []
    for index, conf in enumerate(value):
        reason = service_config_record_violation(capability, conf, index)
        if reason:
            reasons.append(reason)
    return "；".join(reasons) if reasons else None


def service_config_record_violation(
    capability: str, conf: Any, index: int
) -> Optional[str]:
    """
    判定单条服务配置的内容是否合其类型声明的契约

    :param capability: 该配置所属服务族的能力标签
    :param conf: 单条服务配置
    :param index: 该配置在列表中的序号，供无名配置定位
    :return: 面向用户的拒绝原因；合契约或该类型未声明契约时为 None
    """
    if not isinstance(conf, Mapping):
        return f"{_family_label(capability)}第 {index + 1} 条配置应为对象"
    entry = service_instance_registry.find(capability, conf.get("type"))
    if entry is None or entry.config_schema is None:
        return None
    violations = config_value_violations(entry.config_schema, conf.get("config"))
    if not violations:
        return None
    return (
        f"{_family_label(capability)}「{_record_label(conf, index)}」"
        f"（类型 {entry.name}）配置有误：{'；'.join(violations)}"
    )


def service_config_records(capability: str, value: Any) -> List[dict]:
    """
    把一份整族服务配置整形为服务实例配置表的行

    字段按消费方分流：类型实现自己读的内容进 ``config``，宿主自己读的实例级字段进
    ``host_config``，两者都不与身份三元组混放。宿主载荷取该族配置模型声明的字段，
    模型之外的顶层键不入库——这些键在切表前就没有任何读取方（配置模型一律忽略未声明
    字段），留着只会把 ``host_config`` 变成第二个什么都往里塞的 JSON 大对象，而搬出
    大对象正是切表要换的东西。``provider`` 按服务实例登记表当下的归属回填，登记表查
    不到该类型时交由持久化层决定。

    取不到实例名或类型标识的条目不产出行：这类条目在切表前就不产出任何实例，也无法被
    显式指定或被默认调用目标裁决选中，而表按 ``(capability, type, name)`` 定位一行，
    装不下没有身份的条目。同身份的条目后者覆盖前者，与读取端「同名配置后者胜出」一致。

    ``default`` 为真的条目可能有多条也可能一条都没有，这里裁出至多一条：取顺序上第一条
    为真的，与运行期原本就取首个默认标记的行为一致，且同一份输入重复整形结果相同。

    :param capability: 该族的能力标签
    :param value: 整族配置值
    :return: 服务实例配置表的行，每项含 type/name/enabled/config/host_config/
        is_default_target/provider
    """
    host_fields = service_host_fields(capability)
    records: Dict[tuple, dict] = {}
    for conf in value if isinstance(value, list) else []:
        if not isinstance(conf, Mapping):
            continue
        name = conf.get("name")
        service_type = conf.get("type")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(service_type, str) or not service_type.strip():
            continue
        name = name.strip()
        service_type = service_type.strip()
        host_config = {
            field: conf[field] for field in host_fields if conf.get(field) is not None
        }
        records[(service_type, name)] = {
            "type": service_type,
            "name": name,
            "enabled": bool(conf.get("enabled")),
            "config": conf.get("config") or {},
            "host_config": host_config or None,
            "is_default_target": bool(conf.get("default")),
            "provider": _provider_of(capability, service_type),
        }
    default_seen = False
    for record in records.values():
        if not record["is_default_target"]:
            continue
        if default_seen:
            record["is_default_target"] = False
            continue
        default_seen = True
    return list(records.values())


def _provider_of(capability: str, service_type: str) -> Optional[str]:
    """
    取该类型当前的提供方标识

    登记表查不到该类型时返回 None，由持久化层填内建保留值：保留值的取值属于表结构，
    运行期不该持有它的字面量，否则两处各存一份、改一处就漂移。

    :param capability: 能力标签
    :param service_type: 类型标识
    :return: 提供该类型的扩展实例键；登记表查不到时为 None
    """
    entry = service_instance_registry.find(capability, service_type)
    return entry.owner if entry is not None else None


def _record_label(conf: Mapping[str, Any], index: int) -> str:
    """
    取单条服务配置面向用户的称呼

    :param conf: 单条服务配置
    :param index: 该配置在列表中的序号
    :return: 实例名；未取到名字时为按序号的称呼
    """
    name = conf.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return f"第 {index + 1} 条"


def _family_label(capability: str) -> str:
    """
    取服务族面向用户的展示名称

    :param capability: 能力标签
    :return: 族的展示名称；未登记时回落为能力标签本身
    """
    entry = service_family_registry.find(capability)
    return entry.name if entry else capability
