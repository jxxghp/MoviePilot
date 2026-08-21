"""存储实例配置的解析与裸令牌裁决。

一个存储类型可配置多份具名实例——多个网盘账号、多份挂载、多个中转服务——实例名与
存储类型拼成存储令牌 ``u115@work``；不带实例名的裸令牌 ``u115`` 指向该存储类型的
默认实例。

配置由服务实例配置表按族标识 ``storage`` 承载，与下载器、媒体服务器、消息渠道同表
同一套整形：一行一个实例，``type`` 是存储类型、``name`` 是实例名、``config`` 是该实例
的配置内容。整形规则（分列、实例名回落、默认标记裁剪）与三族共用一份实现，收在
`app.runtime.extensions.service_config_validation`；存储族与三族的差别只是默认标记的
作用域是类型而不是族，那条差别由族落点规则声明，不在本模块另写一套。

本模块只剩两件存储族自己的事：把原始条目解析成配置对象，以及按令牌选出目标实例。
"""

from collections.abc import Mapping
from typing import Any, List, Optional

from pydantic import ValidationError

from app.runtime.log import logger
from app.schemas.system import StorageConf as _SchemaStorageConf


def parse_storage_configs(value: Any) -> List[_SchemaStorageConf]:
    """
    把存储实例配置的原始条目逐条解析为配置对象

    一条坏配置只跳过它自己，不影响同族其它实例。

    :param value: 存储实例配置条目，接受配置对象或配置字典组成的列表
    :return: 通过结构校验的配置列表；入参不是列表时为空列表
    """
    confs: List[_SchemaStorageConf] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, _SchemaStorageConf):
            confs.append(item)
            continue
        if not isinstance(item, Mapping):
            logger.warning(f"存储配置格式不正确，已跳过：{item}")
            continue
        try:
            confs.append(_SchemaStorageConf(**item))
        except ValidationError as err:
            logger.error(f"存储配置 {item.get('name')} 校验失败，已跳过：{err}")
    return confs


def select_storage_config(
    confs: List[_SchemaStorageConf], instance: Optional[str]
) -> Optional[_SchemaStorageConf]:
    """
    在同一存储类型的实例配置中选出令牌指向的那一份

    默认实例的裁决与存储后端注册表同一套规则：未具名配置占据默认实例位，优先命中；
    全部具名时只认唯一一个自称默认的；没有默认、或多份同时自称默认，一律认定为无
    默认，绝不按顺序取第一份。

    :param confs: 同一存储类型的实例配置
    :param instance: 实例名，为 None 时取该存储类型的默认实例
    :return: 选中的实例配置；该实例未配置或裁决不出默认实例时为 None
    """
    if instance is not None:
        return next((conf for conf in confs if conf.name == instance), None)
    unnamed = next((conf for conf in confs if not conf.name), None)
    if unnamed is not None:
        return unnamed
    marked = [conf for conf in confs if conf.is_default]
    return marked[0] if len(marked) == 1 else None
