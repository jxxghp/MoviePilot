"""存储实例配置的形状与整形规则。

一个存储类型可配置多份具名实例——多个网盘账号、多份挂载、多个中转服务——实例名与
存储类型拼成存储令牌 ``u115@work``；不带实例名的裸令牌 ``u115`` 指向该存储类型的
默认实例。

配置由服务实例配置表按族标识 ``storage`` 承载，与下载器、媒体服务器、消息渠道同表
不同族：一行一个实例，``type`` 是存储类型、``name`` 是实例名、``config`` 是该实例的
配置内容。默认实例标记落在实例级宿主载荷 ``host_config`` 上，不占用「默认调用目标」
列——后者每族至多一行，而存储的默认实例是每个存储类型各一个。
"""

from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from app.runtime.log import logger
from app.schemas.system import StorageConf as _SchemaStorageConf

# 存储实例配置在服务实例配置表中的族标识
STORAGE_CAPABILITY = "storage"

# 默认实例标记在实例级宿主载荷中的键
DEFAULT_INSTANCE_FIELD = "is_default"


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


def storage_config_records(confs: Iterable[_SchemaStorageConf]) -> List[dict]:
    """
    把全部存储实例配置整形为服务实例配置表的行

    取不到存储类型的条目丢弃，表按 ``(族, 类型, 实例名)`` 定位一行，装不下没有类型的
    条目；未填实例名的条目以存储类型为实例名；同类型同名的条目后者覆盖前者。

    每个存储类型都裁出恰好一个默认实例：有自称默认的取顺序上第一份，一份都没有自称
    时取该类型顺序上第一份。裸令牌 ``u115`` 必须始终指得到一个实例，否则该存储类型
    已有的路径会整体失效。

    存储实例没有启用开关，配了即生效，因此行一律写为已启用。

    :param confs: 全部存储实例配置
    :return: 服务实例配置表的行，每项含 type/name/enabled/config/host_config/
        is_default_target
    """
    records: Dict[Tuple[str, str], dict] = {}
    for conf in confs:
        storage_id = (conf.type or "").strip()
        if not storage_id:
            continue
        name = (conf.name or "").strip() or storage_id
        records[(storage_id, name)] = {
            "type": storage_id,
            "name": name,
            "enabled": True,
            "config": conf.config or {},
            "host_config": {DEFAULT_INSTANCE_FIELD: bool(conf.is_default)},
            "is_default_target": False,
        }
    for storage_id in dict.fromkeys(key[0] for key in records):
        siblings = [record for key, record in records.items() if key[0] == storage_id]
        marked = [
            record for record in siblings
            if record["host_config"][DEFAULT_INSTANCE_FIELD]
        ]
        chosen = marked[0] if marked else siblings[0]
        for record in siblings:
            record["host_config"] = {DEFAULT_INSTANCE_FIELD: record is chosen}
    return list(records.values())


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
