import time
from typing import List, Optional

from app.db.base import DbOper
from app.db.models.pluginconfig import PluginConfig


class PluginConfigOper(DbOper):
    """
    插件实例配置数据管理。
    """

    @staticmethod
    def _now() -> str:
        """返回数据库统一使用的当前时间字符串。"""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def get(self, plugin_id: str, instance_id: str) -> Optional[PluginConfig]:
        """
        按插件标识与实例标识取单条配置。
        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        :return: 命中的配置行，不存在返回 None
        """
        return PluginConfig.get_by_instance(self._db, plugin_id, instance_id)

    async def async_get(self, plugin_id: str, instance_id: str) -> Optional[PluginConfig]:
        """
        异步按插件标识与实例标识取单条配置。
        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        :return: 命中的配置行，不存在返回 None
        """
        return await PluginConfig.async_get_by_instance(self._db, plugin_id, instance_id)

    def list_by_plugin(self, plugin_id: str) -> List[PluginConfig]:
        """
        列出某插件的全部实例配置。
        :param plugin_id: 插件标识
        :return: 该插件全部实例配置行
        """
        return PluginConfig.list_by_plugin(self._db, plugin_id)

    async def async_list_by_plugin(self, plugin_id: str) -> List[PluginConfig]:
        """
        异步列出某插件的全部实例配置。
        :param plugin_id: 插件标识
        :return: 该插件全部实例配置行
        """
        return await PluginConfig.async_list_by_plugin(self._db, plugin_id)

    def list_enabled(self) -> List[PluginConfig]:
        """
        列出全部已启用的实例配置，不限插件。
        :return: 已启用的实例配置行
        """
        return PluginConfig.list_enabled(self._db)

    async def async_list_enabled(self) -> List[PluginConfig]:
        """
        异步列出全部已启用的实例配置，不限插件。
        :return: 已启用的实例配置行
        """
        return await PluginConfig.async_list_enabled(self._db)

    def get_default_target(self, plugin_id: str) -> Optional[PluginConfig]:
        """
        取某插件的默认调用目标实例。
        :param plugin_id: 插件标识
        :return: 置位的配置行，未设置默认调用目标时返回 None
        """
        return PluginConfig.get_default_target(self._db, plugin_id)

    async def async_get_default_target(self, plugin_id: str) -> Optional[PluginConfig]:
        """
        异步取某插件的默认调用目标实例。
        :param plugin_id: 插件标识
        :return: 置位的配置行，未设置默认调用目标时返回 None
        """
        return await PluginConfig.async_get_default_target(self._db, plugin_id)

    def set_default_target(self, plugin_id: str, instance_id: str) -> bool:
        """
        把某插件的默认调用目标改为指定实例，同一事务内清除该插件原有的置位。
        :param plugin_id: 插件标识
        :param instance_id: 要设为默认调用目标的实例标识
        :return: 目标实例存在并完成置位时为 True
        """
        return bool(PluginConfig.set_default_target(self._db, plugin_id, instance_id))

    async def async_set_default_target(self, plugin_id: str, instance_id: str) -> bool:
        """
        异步把某插件的默认调用目标改为指定实例。
        :param plugin_id: 插件标识
        :param instance_id: 要设为默认调用目标的实例标识
        :return: 目标实例存在并完成置位时为 True
        """
        return bool(await PluginConfig.async_set_default_target(self._db, plugin_id, instance_id))

    def clear_default_target(self, plugin_id: str) -> int:
        """
        清除某插件的默认调用目标置位。
        :param plugin_id: 插件标识
        :return: 清除的行数
        """
        return PluginConfig.clear_default_target(self._db, plugin_id)

    def upsert(self, plugin_id: str, instance_id: str, payload: dict) -> PluginConfig:
        """
        写入或更新单个实例配置，不存在则新建。

        payload 中的字段按原样整体写入（含 None），由调用方决定是否携带某字段；
        创建时的 created_at 与每次写入的 updated_at 由本方法统一维护。
        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        :param payload: 待写入的字段值，键为列名
        :return: 写入后的配置行
        """
        now = self._now()
        config = PluginConfig.get_by_instance(self._db, plugin_id, instance_id)
        if config:
            return self._stage_update(config, {**payload, "updated_at": now})
        config = PluginConfig(
            plugin_id=plugin_id,
            instance_id=instance_id,
            created_at=now,
            updated_at=now,
            **payload,
        )
        return self._stage_create(config)

    async def async_upsert(self, plugin_id: str, instance_id: str, payload: dict) -> PluginConfig:
        """
        异步写入或更新单个实例配置，不存在则新建。
        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        :param payload: 待写入的字段值，键为列名
        :return: 写入后的配置行
        """
        now = self._now()
        config = await PluginConfig.async_get_by_instance(self._db, plugin_id, instance_id)
        if config:
            return await self._stage_async_update(config, {**payload, "updated_at": now})
        config = PluginConfig(
            plugin_id=plugin_id,
            instance_id=instance_id,
            created_at=now,
            updated_at=now,
            **payload,
        )
        return await self._stage_async_create(config)

    def delete_instance(self, plugin_id: str, instance_id: str) -> bool:
        """
        删除单个实例配置。
        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        :return: 是否删除了记录
        """
        return bool(PluginConfig.delete_by_instance(self._db, plugin_id, instance_id))

    async def async_delete_instance(self, plugin_id: str, instance_id: str) -> bool:
        """
        异步删除单个实例配置。
        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        :return: 是否删除了记录
        """
        return bool(await PluginConfig.async_delete_by_instance(self._db, plugin_id, instance_id))

    def delete_by_plugin(self, plugin_id: str) -> int:
        """
        删除某插件的全部实例配置。
        :param plugin_id: 插件标识
        :return: 删除的行数
        """
        return PluginConfig.delete_by_plugin(self._db, plugin_id)

    async def async_delete_by_plugin(self, plugin_id: str) -> int:
        """
        异步删除某插件的全部实例配置。
        :param plugin_id: 插件标识
        :return: 删除的行数
        """
        return await PluginConfig.async_delete_by_plugin(self._db, plugin_id)
