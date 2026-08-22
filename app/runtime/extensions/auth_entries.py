"""登录入口：把登录认证族的实例配置与类型登记投影成登录页可展示的入口列表。

登录入口是服务实例族的一员——用户在一份配置列表里配 N 条，宿主扇出 N 个具名入口，
筛选与单实例裁决共用 `app.runtime.extensions.service_config` 那一份实现。本模块只
多做两件该族独有的事：

- **不构造实例。** 登录页在任何用户会话之前渲染，是所有登录方式的唯一入口；一次实例
  构造失败若能让整族入口消失，用户就再也进不来。因此入口描述只取「配置 + 类型登记」
  这两样纯数据，构造留给真正要完成认证握手的那条路径。
- **裁决身份标识。** 入口标识即写进第三方身份绑定表 ``provider`` 列的取值，是绑定
  唯一键的一半。实例配置可显式指定它（承接分身时代的存量绑定），未指定时按
  ``类型@实例名`` 派生；两条配置落到同一个取值就是身份歧义，两条一并不产出入口——
  让其中一条胜出，等于把另一台服务器的用户静默并进这一台的身份命名空间。

逐条错误隔离在两个粒度上都成立：单个类型裁决失败或读取出错只让该类型的入口消失，
同族其余类型照常产出；单条配置身份不成立只影响它自己。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.runtime.extensions.service_config import (
    AUTH_CAPABILITY,
    select_instance_configs,
    service_capability_configs,
)
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.runtime.log import logger

# 入口标识未显式指定时的派生形状，与存储令牌 ``u115@work`` 同形
_DERIVED_IDENTITY_FORMAT = "{service_type}@{name}"

# 已就「多条配置抢同一个入口标识」告警过的标识，避免登录页每次刷新都刷屏
_ambiguous_identities_seen: set = set()


@dataclass(frozen=True, slots=True)
class AuthEntry:
    """一条登录入口，即登录认证族的一条实例配置活的投影。

    :param identity: 入口标识，同时是身份绑定表 ``provider`` 列的取值
    :param service_type: 登录入口类型标识
    :param name: 实例名，即登录页上该入口的名称
    :param icon: 类型展示图标，声明方未给出时为 None
    :param owner: 提供该类型的扩展实例键
    """

    identity: str
    service_type: str
    name: str
    icon: Optional[str] = None
    owner: str = ""


def auth_entry_identity(conf: Any, service_type: str, name: str) -> str:
    """取一条登录入口配置的入口标识。

    显式指定优先：该取值是身份绑定唯一键的一半，一旦变动即换了一个身份命名空间，
    因此必须留一个用户说了算的入口——把它填成分身时代那个入口用过的旧标识，存量绑定
    即继续命中。未指定时按 ``类型@实例名`` 派生，取值在同族内天然唯一，因为实例配置
    表按 ``(能力标签, 类型, 实例名)`` 唯一。

    :param conf: 单条登录入口配置
    :param service_type: 类型标识
    :param name: 实例名
    :return: 入口标识
    """
    declared = getattr(conf, "identity_provider", None)
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return _DERIVED_IDENTITY_FORMAT.format(service_type=service_type, name=name)


def list_auth_entries() -> List[AuthEntry]:
    """列出当前已配置且类型可用的全部登录入口。

    类型登记是入口的准入门槛：类型登记随声明它的扩展启停，扩展没在跑就没有人能完成
    这个入口的认证握手，此时把入口留在登录页上只会让用户点进一个死胡同。

    排列顺序取用户配置的先后而不是类型的登记先后：登录页上按钮的次序用户看得见，
    而宿主内部谁先登记他既看不见也无法预期，那正是 §7.2 禁掉的那类挑选。

    :return: 登录入口列表，按该族配置的先后排列
    """
    try:
        configs = service_capability_configs(AUTH_CAPABILITY) or []
    except Exception as error:
        logger.error(f"【认证】读取登录入口配置出错，登录页只保留系统内建入口：{error}")
        return []
    entries: List[AuthEntry] = []
    for registration in service_instance_registry.adapters(AUTH_CAPABILITY):
        entries.extend(_type_entries(configs, registration.entry))
    order = {
        (getattr(conf, "type", None), getattr(conf, "name", None)): index
        for index, conf in enumerate(configs)
    }
    entries.sort(key=lambda entry: order.get((entry.service_type, entry.name), len(order)))
    return _drop_ambiguous_identities(entries)


def _type_entries(configs: List[Any], registration: Any) -> List[AuthEntry]:
    """按一个登录入口类型的登记与全族配置产出该类型的入口。

    :param configs: 登录认证族已通过结构校验的全部配置
    :param registration: 该类型在服务实例登记表中的登记项
    :return: 该类型的登录入口列表；裁决失败或出错时为空列表
    """
    try:
        selected = select_instance_configs(
            configs,
            registration.service_type,
            capability=AUTH_CAPABILITY,
            multi_instance=registration.multi_instance,
        )
    except LookupError as error:
        logger.error(
            f"【认证】扩展 {registration.owner} 提供的登录入口类型 "
            f"{registration.service_type}（{registration.name}）暂不可用：{error}"
        )
        return []
    except Exception as error:
        logger.error(
            f"【认证】筛选登录入口类型 {registration.service_type} 的配置出错，"
            f"该类型的入口暂不可用：{error}"
        )
        return []
    return [
        AuthEntry(
            identity=auth_entry_identity(conf, registration.service_type, name),
            service_type=registration.service_type,
            name=name,
            icon=registration.icon,
            owner=registration.owner,
        )
        for name, conf in selected.items()
    ]


def _drop_ambiguous_identities(entries: List[AuthEntry]) -> List[AuthEntry]:
    """剔除被多条配置同时认领的入口标识。

    不做裁决而是两条一并剔除：入口标识决定身份绑定落在哪个命名空间，挑一条胜出等于
    把另一条那台服务器的账号静默并进胜出者的身份空间，而这类错误在用户看来就是「登进
    了别人的账号」。剔除是可见的——入口从登录页上消失，日志里带着冲突方与改法。

    :param entries: 已产出的全部登录入口
    :return: 标识唯一的入口列表
    """
    claimants: Dict[str, List[Tuple[str, str]]] = {}
    for entry in entries:
        claimants.setdefault(entry.identity, []).append((entry.service_type, entry.name))
    ambiguous = {identity for identity, claims in claimants.items() if len(claims) > 1}
    for identity in sorted(ambiguous):
        if identity in _ambiguous_identities_seen:
            continue
        _ambiguous_identities_seen.add(identity)
        logger.error(
            f"【认证】登录入口标识 {identity} 被 {len(claimants[identity])} 条配置同时认领："
            f"{claimants[identity]}；该标识是第三方身份绑定的一半，两条共用即两台服务器的"
            f"账号落进同一个身份空间，因此这几条配置都不产出登录入口。请把其中至多一条的"
            f"身份绑定标识填成该取值，其余留空或另填"
        )
    return [entry for entry in entries if entry.identity not in ambiguous]
