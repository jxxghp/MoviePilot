"""服务配置写入前的契约判定。

写入路径与实例构造路径判定的是同一件事——一条服务配置的内容合不合它那个类型声明的
契约——但站位不同：写入路径能在配置落盘前把错误退回给用户并说明原因，构造路径只能
在配置已经存下去之后跳过这一条。两者共用同一个判定函数，因此不会出现「写得进去、
用不起来」的分歧。

本模块把「配置键属于哪一族」与「该族里某个类型声明了什么契约」两件事接起来，因而
同时依赖配置读取端口与服务实例登记表；后者依赖前者，故这层接线不能收回任一侧，只能
另立一处。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, List, Optional

from app.runtime.extensions.config_schema import config_value_violations
from app.runtime.extensions.service_config import service_capability
from app.runtime.extensions.service_family_registry import service_family_registry
from app.runtime.extensions.service_instance_registry import service_instance_registry


def service_config_write_violation(config_key: Any, value: Any) -> Optional[str]:
    """
    判定一次服务配置写入是否合各条目所属类型声明的契约

    整条写入要么全进要么全退：写入端点收到的是整份配置列表，只挑掉不合契约的那几条
    等于替用户丢数据，而用户既看不到丢了什么也无从恢复。因此一条不合契约即退回整次
    写入，并逐条说明是哪个实例的哪个字段有问题。

    不属于任何服务族的配置键、以及未登记或未声明契约的类型都不产生判定，行为与本
    判定加入之前完全一致。

    :param config_key: 被写入的配置键，接受 `SystemConfigKey` 成员或其取值字符串
    :param value: 待写入的配置值
    :return: 面向用户的拒绝原因；可以写入时为 None
    """
    capability = service_capability(_key_text(config_key))
    if not capability:
        return None
    if value is None:
        return None
    if not isinstance(value, list):
        return f"{_family_label(capability)}配置应为列表，实际为 {type(value).__name__}"
    reasons: List[str] = []
    for index, conf in enumerate(value):
        reason = _record_violation(capability, conf, index)
        if reason:
            reasons.append(reason)
    return "；".join(reasons) if reasons else None


def _record_violation(capability: str, conf: Any, index: int) -> Optional[str]:
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


def _key_text(config_key: Any) -> Optional[str]:
    """
    把配置键入参归一为字符串

    :param config_key: `SystemConfigKey` 成员或其取值字符串
    :return: 配置键取值；取不到时为 None
    """
    value = getattr(config_key, "value", config_key)
    return value if isinstance(value, str) else None
