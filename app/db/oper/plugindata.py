from typing import Any, Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.plugindata import PluginData
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


class PluginDataOper(DbOper):
    """
    插件数据管理
    """

    def save(self, plugin_id: str, key: str, value: Any, instance_id: str = DEFAULT_INSTANCE_ID):
        """
        保存插件数据
        :param plugin_id: 插件id
        :param key: 数据key
        :param value: 数据值
        :param instance_id: 实例标识，默认取默认实例
        """
        plugin = PluginData.get_plugin_data_by_key(self._db, plugin_id, key, instance_id)
        if plugin:
            self._stage_update(plugin, {
                "value": value
            })
        else:
            self._stage_create(
                PluginData(plugin_id=plugin_id, instance_id=instance_id, key=key, value=value)
            )

    async def async_save(self, plugin_id: str, key: str, value: Any,
                          instance_id: str = DEFAULT_INSTANCE_ID) -> None:
        """
        异步保存插件数据

        :param plugin_id: 插件ID
        :param key: 数据键
        :param value: 数据值
        :param instance_id: 实例标识，默认取默认实例
        """
        plugin = await PluginData.async_get_plugin_data_by_key(
            self._db, plugin_id, key, instance_id
        )
        if plugin:
            await self._stage_async_update(plugin, {"value": value})
        else:
            await self._stage_async_create(
                PluginData(plugin_id=plugin_id, instance_id=instance_id, key=key, value=value)
            )

    def get_data(self, plugin_id: str, key: Optional[str] = None,
                 instance_id: str = DEFAULT_INSTANCE_ID) -> Any:
        """
        获取插件数据
        :param plugin_id: 插件id
        :param key: 数据key，为空时返回该实例下该插件的全部数据
        :param instance_id: 实例标识，默认取默认实例
        """
        if key:
            data = PluginData.get_plugin_data_by_key(self._db, plugin_id, key, instance_id)
            if not data:
                return None
            return data.value
        else:
            return PluginData.get_plugin_data(self._db, plugin_id, instance_id)

    async def async_get_data(self, plugin_id: str, key: Optional[str] = None,
                              instance_id: str = DEFAULT_INSTANCE_ID) -> Any:
        """
        异步获取插件数据。
        :param plugin_id: 插件id
        :param key: 数据key，为空时返回该实例下该插件的全部数据
        :param instance_id: 实例标识，默认取默认实例
        """
        if key:
            data = await PluginData.async_get_plugin_data_by_key(
                self._db, plugin_id, key, instance_id
            )
            if not data:
                return None
            return data.value
        return await PluginData.async_get_plugin_data(self._db, plugin_id, instance_id)

    def del_data(self, plugin_id: str, key: Optional[str] = None,
                 instance_id: Optional[str] = None) -> Any:
        """
        删除插件数据

        与查询类方法的默认范围刻意不对称：查询默认限定当前实例，本方法默认
        ``instance_id=None`` 即跨该插件全部实例删除。卸载插件需要清空它在宿主内的
        全部实例数据，若删除也默认只清默认实例，会在卸载后留下其余实例的残余数据；
        需要只清某一个实例时显式传入该实例标识。
        :param plugin_id: 插件id
        :param key: 数据key，为空时删除该范围下的全部数据
        :param instance_id: 实例标识，为 None 时跨全部实例删除
        """
        def stage(session: Session) -> None:
            """把兼容删除入口映射到调用方或组合根持有的事务。"""
            if key:
                PluginData.del_plugin_data_by_key(session, plugin_id, key, instance_id)
            else:
                PluginData.del_plugin_data(session, plugin_id, instance_id)

        self._execute_sync_write(stage)

    def stage_delete(self, plugin_id: str) -> None:
        """暂存目标插件全部数据删除并 flush，不提交调用方事务。

        与 ``del_data`` 不同，本方法跨该插件全部实例删除且不接受 ``instance_id``
        收窄——它服务的是插件卸载场景，需要清空该插件在宿主内的全部实例数据。
        """
        if not isinstance(self._db, Session):
            raise RuntimeError("插件数据暂存删除需要调用方提供 Session")
        self._db.execute(
            delete(PluginData).where(PluginData.plugin_id == plugin_id)
        )
        self._db.flush()

    def truncate(self):
        """
        清空插件数据
        """
        self._stage_truncate(PluginData)

    def get_data_all(self, plugin_id: str, instance_id: str = DEFAULT_INSTANCE_ID) -> Any:
        """
        获取插件所有数据
        :param plugin_id: 插件id
        :param instance_id: 实例标识，默认取默认实例
        """
        return PluginData.get_plugin_data_by_plugin_id(self._db, plugin_id, instance_id)

    async def async_get_data_all(self, plugin_id: str, instance_id: str = DEFAULT_INSTANCE_ID) -> Any:
        """
        异步获取插件所有数据。
        :param plugin_id: 插件id
        :param instance_id: 实例标识，默认取默认实例
        """
        return await PluginData.async_get_plugin_data_by_plugin_id(self._db, plugin_id, instance_id)
