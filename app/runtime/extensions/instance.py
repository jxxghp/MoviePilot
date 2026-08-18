"""扩展实例在运行期的标识与定位。

一个扩展可以按配置扇出为多个实例，每个实例由 ``(扩展标识, 实例标识)`` 唯一确定。
实例键把这两段压成一个字符串，作为该实例在宿主内的稳定标识
（即 ``ExtensionView.extension_id`` 的取值形式），运行态表、注册来源与生命周期开关
都以它定位一个实例。默认实例的实例键退化为裸扩展标识，因此单实例扩展的取值与
不区分实例时完全一致。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Optional, TypeVar

# 未创建分身时实例所用的实例标识
DEFAULT_INSTANCE_ID = "default"

# 实例标识的合法字符集：实例标识同时作为数据目录名的一段，
# 不接受路径分隔符与点号，避免拼接出目录树之外的路径
INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 实例键中扩展标识与实例标识的分隔符，扩展标识与实例标识的字符集都不含该字符
INSTANCE_KEY_SEPARATOR = "@"

T = TypeVar("T")

__all__ = [
    "DEFAULT_INSTANCE_ID",
    "INSTANCE_ID_PATTERN",
    "INSTANCE_KEY_SEPARATOR",
    "extension_id_of",
    "instance_key",
    "is_default_instance_key",
    "matches_extension",
    "normalize_instance_id",
    "resolve_running_instance",
    "split_instance_key",
]


def normalize_instance_id(instance_id: Optional[str]) -> str:
    """校验实例标识并返回归一后的取值。

    :param instance_id: 待校验的实例标识，为空时取默认实例
    :return: 合法的实例标识
    :raises ValueError: 实例标识含非法字符或超长
    """
    if not instance_id:
        return DEFAULT_INSTANCE_ID
    if not INSTANCE_ID_PATTERN.match(instance_id):
        raise ValueError(f"非法的扩展实例标识：{instance_id}")
    return instance_id


def instance_key(extension_id: str, instance_id: Optional[str] = None) -> str:
    """组合扩展实例在运行期的唯一键。

    :param extension_id: 扩展标识
    :param instance_id: 实例标识，为空时取默认实例
    :return: 默认实例返回裸扩展标识，其余返回 ``extension_id@instance_id``
    :raises ValueError: 实例标识含非法字符或超长
    """
    normalized = normalize_instance_id(instance_id)
    if normalized == DEFAULT_INSTANCE_ID:
        return extension_id
    return f"{extension_id}{INSTANCE_KEY_SEPARATOR}{normalized}"


def split_instance_key(key: str) -> tuple[str, str]:
    """反解实例键。

    :param key: 实例键
    :return: ``(扩展标识, 实例标识)``，裸扩展标识对应默认实例
    """
    extension_id, separator, suffix = key.partition(INSTANCE_KEY_SEPARATOR)
    if not separator or not suffix:
        return key, DEFAULT_INSTANCE_ID
    return extension_id, suffix


def extension_id_of(key: str) -> str:
    """取实例键所属的扩展标识。

    :param key: 实例键
    :return: 扩展标识
    """
    return split_instance_key(key)[0]


def is_default_instance_key(key: str) -> bool:
    """判断实例键是否指向默认实例。

    :param key: 实例键
    :return: 是否为默认实例
    """
    return split_instance_key(key)[1] == DEFAULT_INSTANCE_ID


def matches_extension(key: str, selector: Optional[str]) -> bool:
    """判断实例键是否命中筛选条件。

    :param key: 实例键
    :param selector: 扩展标识或实例键，扩展标识命中该扩展的全部实例，为空时命中全部
    :return: 是否命中
    """
    if not selector:
        return True
    return key == selector or extension_id_of(key) == selector


def resolve_running_instance(running: Mapping[str, T], key: str) -> Optional[T]:
    """在运行态表中定位一个扩展实例。

    优先按实例键精确命中；传入扩展标识且该扩展恰好只有一个实例在运行时回落到该实例，
    因此只创建了分身、没有默认实例的扩展按扩展标识同样能取到。有多个实例在运行时不回落，
    避免按登记顺序取到调用方并未指定的那一个。

    :param running: 运行态表 ``{实例键: 运行实体}``
    :param key: 实例键或扩展标识
    :return: 运行实体，未运行或无法唯一确定时为 None
    """
    entity = running.get(key)
    if entity is not None:
        return entity
    candidates = [
        candidate
        for running_key, candidate in running.items()
        if extension_id_of(running_key) == key
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None
