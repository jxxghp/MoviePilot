"""存储实例配置的读写与存储服务的取用。

两件事各答各的问题。**按配置扇出实例**与下载器、媒体服务器、消息渠道同一条路径：
同一张服务实例配置表、同一套筛选与单实例裁决，取服务经 `get_services()` 与
`get_service()`。**按令牌寻址**是存储独有的一步：存储实例的标识写在持久化路径里
（``u115@work:/media``），故还要回答「这个地址指的配置是哪一份」——裸令牌 ``u115``
落到该存储类型的兼容指针所指的那一份，具名令牌 ``u115@work`` 精确指向该类型下名为
``work`` 的实例。配置的形状、整形规则与兼容指针的退场路径见
``app.application.storage_config``。
"""

from typing import Any, List, Optional

from app.application.service_config import get_configured_service_instance_configs
from app.application.storage_config import (
    parse_storage_configs,
    select_storage_config,
)
from app.runtime.extensions.service_config import STORAGE_CAPABILITY
from app.runtime.extensions.service_registry import ServiceBaseHelper
from app.runtime.log import logger
from app.schemas.file import FileURI as _SchemaFileURI
from app.schemas.system import StorageConf as _SchemaStorageConf
from app.schemas.types import SystemConfigKey


class StorageHelper(ServiceBaseHelper[_SchemaStorageConf]):
    """
    存储帮助类

    继承的部分回答「按配置扇出的实例有哪些」，本类自有的部分回答「某个存储令牌指的
    配置是哪一份」。后者不由前者代答：服务发现按实例名索引整族实例，而令牌里的实例段
    可以缺席，缺席时要按所属存储类型的兼容指针补全。
    """

    def __init__(self):
        """绑定存储配置键与配置模型。"""
        super().__init__(
            config_key=SystemConfigKey.Storages,
            conf_type=_SchemaStorageConf,
        )

    @staticmethod
    def get_storagies() -> List[_SchemaStorageConf]:
        """
        获取所有存储设置

        :return: 全部存储类型的实例配置，按写入先后排列
        """
        return parse_storage_configs(
            get_configured_service_instance_configs().read(STORAGE_CAPABILITY)
        )

    def list_storages(self, storage_id: str) -> List[_SchemaStorageConf]:
        """
        获取指定存储类型的全部实例配置

        :param storage_id: 存储标识，如 u115
        :return: 该存储类型的实例配置；标识为空时为空列表
        """
        if not storage_id:
            return []
        return [conf for conf in self.get_storagies() if conf.type == storage_id]

    def get_storage(self, storage: str) -> Optional[_SchemaStorageConf]:
        """
        获取指定存储配置

        裸令牌取该存储类型的兼容指针所指的那一份，具名令牌精确取用该实例。

        :param storage: 存储令牌，如 u115 或 u115@work
        :return: 实例配置；令牌不合法或该实例未配置时为 None
        """
        parts = _SchemaFileURI.storage_parts(storage)
        if not parts:
            return None
        return select_storage_config(self.list_storages(parts[0]), parts[1])

    def set_storage(self, storage: str, conf: dict) -> None:
        """
        设置存储配置

        :param storage: 存储令牌，如 u115 或 u115@work
        :param conf: 配置内容
        """
        self._write_config(storage, conf)

    def add_storage(self, storage: str, name: str, conf: dict) -> None:
        """
        添加存储配置

        :param storage: 存储标识，如 u115
        :param name: 实例名
        :param conf: 配置内容
        """
        self.save_storagies(
            self.get_storagies()
            + [_SchemaStorageConf(type=storage, name=name, config=conf)]
        )

    def reset_storage(self, storage: str) -> None:
        """
        重置置配置

        :param storage: 存储令牌，如 u115 或 u115@work
        """
        self._write_config(storage, {})

    @staticmethod
    def save_storagies(value: Any) -> bool:
        """
        用给定的整族存储配置覆盖现有配置

        :param value: 整族存储实例配置，接受配置对象或配置字典，为 None 时视为清空
        :return: 配置内容是否发生变化
        """
        return get_configured_service_instance_configs().save(
            STORAGE_CAPABILITY,
            [conf.model_dump() for conf in parse_storage_configs(value)],
        )

    def _write_config(self, storage: str, conf: dict) -> None:
        """
        写入存储令牌指向的那个实例的配置内容

        令牌指向的实例尚未配置时建出来：裸令牌在该存储类型一份配置都没有时建出承接
        裸令牌的那一份，具名令牌建出同名实例。该存储类型已有配置却裁决不出兼容指针时
        不写入——写下去只会把内容落到用户没有指定的实例上。

        :param storage: 存储令牌，如 u115 或 u115@work
        :param conf: 配置内容
        """
        parts = _SchemaFileURI.storage_parts(storage)
        if not parts:
            logger.error(f"存储令牌 {storage} 不合法，配置未写入")
            return
        storage_id, instance = parts
        confs = self.get_storagies()
        siblings = [item for item in confs if item.type == storage_id]
        target = select_storage_config(siblings, instance)
        if target is None:
            if instance is None and siblings:
                logger.error(f"存储 {storage_id} 裁决不出承接裸令牌的实例，配置未写入")
                return
            confs = confs + [_SchemaStorageConf(
                type=storage_id,
                name=instance or storage_id,
                bare_token_target=instance is None,
                config=conf,
            )]
        else:
            confs = [
                item.model_copy(update={"config": conf}) if item is target else item
                for item in confs
            ]
        self.save_storagies(confs)
