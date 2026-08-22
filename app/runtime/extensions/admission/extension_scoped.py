"""扩展级声明在同一扩展多个实例之间的去重裁决。

扩展有两种彼此正交的「多」。一种是扩展按配置扇出多个实例，实例之间是各自独立的
行为体，声明什么都算各自的事；另一种是扩展只提供一种新类型，用户要接入多份同类
配置时配的是该类型自己的配置，不是多个扩展实例。后者声明的是「本宿主提供这个
标识」这件进程级事实，与该扩展建了几个实例无关，因此同一扩展的多个实例声明同一
标识时只认一次。

认哪一次不取决于实例的启动顺序：默认实例优先，其余按实例标识升序，取第一个。规则
只读实例键本身，任何登记顺序都得到同一个结果。归属仍记到具体实例键，回收按实例键
精确进行；被选中的实例停止或停用后，仍在运行且声明同一标识的兄弟实例重新参与裁决。

不同实例声明不同标识，以及不同扩展声明同一标识，都不在本模块的裁决范围内——前者
本就合法，后者是扩展之间的覆盖规则，由各注册表自行决定。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from app.runtime.extensions.contract.instance import (
    DEFAULT_INSTANCE_ID,
    extension_id_of,
    split_instance_key,
)
from app.runtime.log import logger as default_logger

# 已就「同一扩展的多个实例声明同一标识」告警过的 (扩展标识, 声明钩子, 标识) 组合。
# 投影方法处在取用热路径上会被反复调用，去重状态跨投影实例共享，避免同一提示刷屏。
_extension_scoped_warnings_seen: set[Tuple[str, str, Tuple]] = set()


def instance_precedence(key: str) -> Tuple[bool, str]:
    """返回实例键在扩展级裁决中的排序位次。

    :param key: 实例键
    :return: 排序键，默认实例排在最前，其余按实例标识升序
    """
    _, instance_id = split_instance_key(key)
    return instance_id != DEFAULT_INSTANCE_ID, instance_id


def elect_extension_scoped(
    declared_by_instance: Mapping[str, List[Any]],
    identity_of: Callable[[Any], Optional[Tuple]],
    *,
    subject: str,
    hook: str,
    log: Any = default_logger,
) -> Dict[str, List[Any]]:
    """在同一扩展的多个实例之间只保留一次同标识声明。

    标识推导不出来的声明原样保留，不参与裁决——去重是为了消除重复登记，不是
    为了替契约校验再筛一遍。同一个实例自己重复声明同一标识同样原样保留，那是
    单实例内部的覆盖语义，与实例之间的裁决无关。

    :param declared_by_instance: 实例键到该实例已通过契约校验的声明列表的映射
    :param identity_of: 从单条声明推导标识元组的函数，推导不出时返回 None
    :param subject: 标识在告警文案里的称呼，例如「服务实例类型」
    :param hook: 声明钩子名，用于告警文案与告警去重
    :param log: 日志端口
    :return: 与入参同形的映射，落选实例的同标识声明已被剔除
    """
    identities: Dict[str, List[Optional[Tuple]]] = {
        key: [identity_of(item) for item in items]
        for key, items in declared_by_instance.items()
    }
    claimants: Dict[Tuple[str, Tuple], List[str]] = {}
    for key, key_identities in identities.items():
        extension_id = extension_id_of(key)
        for identity in key_identities:
            if identity is None:
                continue
            claimants.setdefault((extension_id, identity), []).append(key)
    winners = {
        claim: min(keys, key=instance_precedence)
        for claim, keys in claimants.items()
    }
    for claim, keys in claimants.items():
        _warn_duplicate_claim(claim, keys, winners[claim], subject=subject, hook=hook, log=log)
    return {
        key: [
            item
            for item, identity in zip(items, identities[key])
            if identity is None or winners.get((extension_id_of(key), identity)) == key
        ]
        for key, items in declared_by_instance.items()
    }


def _warn_duplicate_claim(
    claim: Tuple[str, Tuple],
    keys: List[str],
    winner: str,
    *,
    subject: str,
    hook: str,
    log: Any,
) -> None:
    """就一个标识被同扩展多个实例声明打一次提示。

    :param claim: (扩展标识, 标识元组)
    :param keys: 声明该标识的实例键列表
    :param winner: 裁决胜出的实例键
    :param subject: 标识在告警文案里的称呼
    :param hook: 声明钩子名
    :param log: 日志端口
    :return: 无返回值
    """
    extension_id, identity = claim
    declarants = list(dict.fromkeys(keys))
    if len(declarants) < 2:
        return
    seen = (extension_id, hook, identity)
    if seen in _extension_scoped_warnings_seen:
        return
    _extension_scoped_warnings_seen.add(seen)
    log.warning(
        f"插件[{extension_id}]有 {len(declarants)} 个实例声明了同一个{subject} "
        f"{'/'.join(str(part) for part in identity)}：{declarants}；"
        f"{hook}() 声明的是「本宿主提供这个{subject}」这件扩展级事实，与该插件建了"
        f"几个实例无关，因此同一标识只登记一次——本次归属 {winner}，其余实例的同标识"
        f"声明已忽略。请只在一个实例上声明它；不同实例声明不同标识不受影响"
    )
