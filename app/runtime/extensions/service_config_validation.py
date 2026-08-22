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

from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional

from app.runtime.extensions.contract.config_schema import config_value_violations
from app.runtime.extensions.service_config import (
    service_bare_token_field,
    service_host_fields,
    service_instance_enabled,
    service_instance_name,
    service_supports_default_target,
)
from app.runtime.extensions.registry.service_family import service_family_registry
from app.runtime.extensions.registry.service_instance import service_instance_registry


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


def service_config_record(capability: str, conf: Any) -> Optional[dict]:
    """
    把单条服务配置整形为服务实例配置表的一行

    字段按消费方分流：类型实现自己读的内容进 ``config``，宿主自己读的实例级字段进
    ``host_config``，两者都不与身份三元组混放。宿主载荷取该族配置模型声明的字段，
    模型之外的顶层键不入库——这些键在切表前就没有任何读取方（配置模型一律忽略未声明
    字段），留着只会把 ``host_config`` 变成第二个什么都往里塞的 JSON 大对象，而搬出
    大对象正是切表要换的东西。``provider`` 按服务实例登记表当下的归属回填，登记表查
    不到该类型时为 None，交由持久化层决定。

    取不到类型标识的条目不产出行：表按 ``(capability, type, name)`` 定位一行，装不下没有
    身份的条目。实例名缺省时是否回落为类型标识由族决定（`service_instance_name`），不回落
    的族里无名条目同样不产出行——这类条目在切表前就不产出任何实例，也无法被显式指定或被
    默认标记裁决选中。

    ``is_default_target`` 按配置自带的标记原样产出，不在此裁决：「每族至多一个默认调用
    目标」与「每个存储类型恰好一个裸令牌兼容指针」都是族范围内的裁决，单条配置里没有
    兄弟条目可比，无从判定，由调用方在族的范围内收口。

    :param capability: 该族的能力标签
    :param conf: 单条服务配置
    :return: 配置行，含 type/name/enabled/config/host_config/is_default_target/provider；
        取不到类型标识或实例名时为 None
    """
    if not isinstance(conf, Mapping):
        return None
    service_type = conf.get("type")
    if not isinstance(service_type, str) or not service_type.strip():
        return None
    service_type = service_type.strip()
    name = service_instance_name(capability, conf.get("name"), service_type)
    if not name:
        return None
    host_config = {
        field: conf[field]
        for field in service_host_fields(capability)
        if conf.get(field) is not None
    }
    return {
        "type": service_type,
        "name": name,
        "enabled": service_instance_enabled(capability, conf),
        "config": conf.get("config") or {},
        "host_config": host_config or None,
        "is_default_target": bool(conf.get("default")),
        "provider": _provider_of(capability, service_type),
    }


def service_config_records(capability: str, value: Any) -> List[dict]:
    """
    把一份整族服务配置整形为服务实例配置表的行

    逐条整形与 `service_config_record` 共用一份实现，本函数只补上族范围内的裁决。
    同身份的条目后者覆盖前者，与读取端「同名配置后者胜出」一致。

    有默认调用目标的族同规格：整族裁出至多一条 ``is_default_target``，取顺序上第一条
    为真的；没有默认调用目标的族整族裁成假。存储另外还要裁出裸令牌兼容指针，它落宿主
    载荷、每个类型恰好一条，与默认调用目标各占各的载体、互不换算。同一份输入重复整形
    结果相同。

    :param capability: 该族的能力标签
    :param value: 整族配置值
    :return: 服务实例配置表的行，每项含 type/name/enabled/config/host_config/
        is_default_target/provider
    """
    records: Dict[tuple, dict] = {}
    for conf in value if isinstance(value, list) else []:
        record = service_config_record(capability, conf)
        if record is None:
            continue
        records[(record["type"], record["name"])] = record
    _trim_default_markers(capability, records)
    return list(records.values())


def _trim_default_markers(capability: str, records: Dict[tuple, dict]) -> None:
    """把整族配置行的默认调用目标裁剪到至多一条，并按需补齐裸令牌兼容指针。

    没有默认调用目标的族一律裁成假：该族的语义里不存在「调用未指定实例」，标记本身
    无从解释，而它又受「每族至多一个」的条件唯一索引管辖，放任写入只会在第二条置位
    时撞索引，整次写入连带失败。

    :param capability: 该族的能力标签
    :param records: 身份二元组到配置行的映射，原地改写
    :return: 无返回值
    """
    supports_default = service_supports_default_target(capability)
    default_seen = False
    for record in records.values():
        if not record["is_default_target"]:
            continue
        if default_seen or not supports_default:
            record["is_default_target"] = False
            continue
        default_seen = True
    field = service_bare_token_field(capability)
    if field:
        _trim_bare_token_pointers(field, records)


def elect_bare_token_holder(
    field: str, records: Sequence[Mapping[str, Any]]
) -> Optional[Mapping[str, Any]]:
    """
    在同一类型的配置行里裁出承接裸令牌兼容指针的那一份

    有自称的取顺序上第一份，一份都没有自称时取顺序上第一份。裁出「恰好一条」而不是
    「至多一条」，是因为存量路径必须始终指得到实例——一份都不标记会让该类型已有的裸
    路径整体失效。整族写入与单条写入共用本裁决，两条写入口因而不会给出不同的指向。

    :param field: 兼容指针在该族配置模型上的字段名
    :param records: 同一类型下的配置行，顺序即写入先后
    :return: 承接兼容指针的那一行；一行都没有时为 None
    """
    if not records:
        return None
    marked = [item for item in records if (item.get("host_config") or {}).get(field)]
    return marked[0] if marked else records[0]


def _trim_bare_token_pointers(field: str, records: Dict[tuple, dict]) -> None:
    """为每个类型裁出恰好一个裸令牌兼容指针，标记落宿主载荷。

    这条兼容层随存量路径补全实例名而退场，届时整个函数与该字段一并移除，默认调用
    目标不受影响。

    :param field: 兼容指针在该族配置模型上的字段名
    :param records: 身份二元组到配置行的映射，原地改写
    :return: 无返回值
    """
    for service_type in dict.fromkeys(key[0] for key in records):
        siblings = [record for key, record in records.items() if key[0] == service_type]
        chosen = elect_bare_token_holder(field, siblings)
        for record in siblings:
            host_config = dict(record["host_config"] or {})
            host_config[field] = record is chosen
            record["host_config"] = host_config


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
