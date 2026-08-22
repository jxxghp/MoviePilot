"""存储实例配置的解析与裸令牌兼容指针裁决。

一个存储类型可配置多份具名实例——多个网盘账号、多份挂载、多个中转服务——实例名与
存储类型拼成存储令牌 ``u115@work``；不带实例名的裸令牌 ``u115`` 落到该存储类型的
兼容指针所指的那一份。

配置由服务实例配置表按族标识 ``storage`` 承载，与下载器、媒体服务器、消息渠道同表
同一套整形：一行一个实例，``type`` 是存储类型、``name`` 是实例名、``config`` 是该实例
的配置内容；默认调用目标也与三族同规格，整族至多一个，由 ``serviceconfig`` 的专列
承载。整形规则（分列、实例名回落、默认标记裁剪）与三族共用一份实现，收在
`app.runtime.extensions.admission.service_config`。

**兼容指针不是默认。** 它只回答一个问题：存量路径 ``u115:/media`` 没有实例段时该落到
哪个实例。这是地址补全，不是「用户没指定存储时用哪个」——后者是族级默认调用目标，
落 ``is_default_target`` 列。一个实例可以同时是族级默认与所在类型的兼容指针，也可以
只是其中之一，两者互不推导。**退场路径**：所有存量路径补全实例名（``u115@主号:/media``）
之后，兼容指针连同 `select_storage_config` 的无实例名分支一并移除，届时取不到实例名
的令牌直接判为不合法，不再有任何回落。

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

    裸令牌的裁决与存储后端注册表同一套规则：未具名配置占据裸令牌位，优先命中；全部
    具名时只认唯一一个自称承接裸令牌的；无人自称、或多份同时自称，一律让出，绝不按
    顺序取第一份。这里读的是兼容指针而不是族级默认调用目标：令牌已经写死了存储类型，
    缺的只是实例段，回答的是地址补全而不是「用户没指定存储时用哪个」。

    :param confs: 同一存储类型的实例配置
    :param instance: 实例名，为 None 时取该存储类型的裸令牌兼容指针所指的那一份
    :return: 选中的实例配置；该实例未配置或裁决不出兼容指针时为 None
    """
    if instance is not None:
        return next((conf for conf in confs if conf.name == instance), None)
    unnamed = next((conf for conf in confs if not conf.name), None)
    if unnamed is not None:
        return unnamed
    marked = [conf for conf in confs if conf.bare_token_target]
    return marked[0] if len(marked) == 1 else None
