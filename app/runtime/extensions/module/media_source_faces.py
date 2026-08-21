"""媒体数据源能力面：按方法名把来源能做的事分组。

一张扁平的来源清单答不了「这个来源能拿来做什么」。只做发现的来源与做元数据识别的
来源混在一起，用户在识别源里选中前者，调用按来源分发下去无人认领，静默落空——不
报错、什么都不发生，看上去像网络问题。

能力面从方法名推导，不由作者另行声明。宿主本就知道每个来源交出了哪些方法，能力索
引也按方法名建；让作者再写一遍能力面，等于把同一件事声明两次，写漏的那一半照样过
校验，正是要消灭的形态。

归入同一面的方法取用形状相同：调用方按来源收窄，实现方按弃权协议让出。收窄键不必
相同——``match_media`` 一族由宿主按 ``source`` 路由，``recognize_media``、
``search_medias`` 与刮削族由实现按 ``media_source``/``scrape_source`` 自认领——但对
用户是同一个选择：这个来源能不能拿来干这件事。按 ``server``/``itemid`` 收窄的
``media_exists`` 问的是「哪台媒体服务器有」，与来源无关，因此不构成任何一个面。

推导只读取已有事实，不参与分发：一个方法在不在某个面里，不改变它被谁调用、何时短路。
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

from app.schemas.types import MediaSourceCapability

_ASYNC_PREFIX = "async_"

# 能力面到其契约方法的划分。元组顺序即对外列举顺序，与方法登记先后无关。
_CAPABILITY_METHODS: Tuple[Tuple[MediaSourceCapability, Tuple[str, ...]], ...] = (
    (
        MediaSourceCapability.RECOGNIZE,
        ("recognize_media", "match_media"),
    ),
    (
        MediaSourceCapability.SEARCH,
        ("search_medias",),
    ),
    (
        MediaSourceCapability.DETAIL,
        ("media_detail", "media_credits", "person_detail", "person_credits"),
    ),
    (
        MediaSourceCapability.RECOMMEND,
        ("media_recommend", "media_similar"),
    ),
    (
        MediaSourceCapability.DISCOVER,
        ("discover", "discover_board"),
    ),
    (
        MediaSourceCapability.SCRAPE,
        ("obtain_images", "metadata_nfo", "metadata_img"),
    ),
)

_METHOD_CAPABILITIES: Dict[str, MediaSourceCapability] = {
    method: capability
    for capability, methods in _CAPABILITY_METHODS
    for method in methods
}

_CAPABILITY_ORDER: Dict[MediaSourceCapability, int] = {
    capability: index for index, (capability, _methods) in enumerate(_CAPABILITY_METHODS)
}


def method_capability(method: str) -> Optional[MediaSourceCapability]:
    """
    返回方法名所属的媒体数据源能力面

    ``async_`` 变体与同名同步方法是同一份契约的两种形态，去前缀后再查表。

    :param method: 模块方法名
    :return: 该方法所属的能力面；不构成来源能力面时为 None
    """
    if not method:
        return None
    base = method[len(_ASYNC_PREFIX):] if method.startswith(_ASYNC_PREFIX) else method
    return _METHOD_CAPABILITIES.get(base)


def ordered_capabilities(
        capabilities: Iterable[MediaSourceCapability],
) -> Tuple[MediaSourceCapability, ...]:
    """
    按固定划分顺序去重排列能力面

    顺序取自能力面划分表本身，与来源登记先后、方法表遍历顺序都无关。

    :param capabilities: 待排列的能力面，允许重复
    :return: 去重并按划分顺序排列的能力面元组
    """
    unique = {
        capability
        for capability in capabilities
        if capability in _CAPABILITY_ORDER
    }
    return tuple(sorted(unique, key=_CAPABILITY_ORDER.__getitem__))


def media_source_capabilities(
        methods: Iterable[str],
) -> Tuple[MediaSourceCapability, ...]:
    """
    从方法名推导媒体数据源占据的能力面

    :param methods: 来源交出的方法名，可含 ``async_`` 变体与不构成能力面的方法名
    :return: 该来源占据的能力面元组，按固定划分顺序排列
    """
    return ordered_capabilities(
        capability
        for method in methods
        if (capability := method_capability(method)) is not None
    )
