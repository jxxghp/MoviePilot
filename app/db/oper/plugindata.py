from typing import Any, Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.plugindata import PluginData


class PluginDataOper(DbOper):
    """
    插件数据管理
    """

    def save(self, plugin_id: str, key: str, value: Any):
        """
        保存插件数据
        :param plugin_id: 插件id
        :param key: 数据key
        :param value: 数据值
        """
        plugin = self._execute_sync_query(
            lambda session: PluginData.get_plugin_data_by_key(
                session, plugin_id, key
            )
        )
        if plugin:
            self._stage_update(plugin, {
                "value": value
            })
        else:
            self._stage_create(PluginData(plugin_id=plugin_id, key=key, value=value))

    async def async_save(self, plugin_id: str, key: str, value: Any) -> None:
        """
        异步保存插件数据

        :param plugin_id: 插件ID
        :param key: 数据键
        :param value: 数据值
        """
        plugin = await self._execute_async_query(
            lambda session: PluginData.async_get_plugin_data_by_key(
                session, plugin_id, key
            )
        )
        if plugin:
            await self._stage_async_update(plugin, {"value": value})
        else:
            await self._stage_async_create(
                PluginData(plugin_id=plugin_id, key=key, value=value)
            )

    def get_data(self, plugin_id: str, key: Optional[str] = None) -> Any:
        """
        获取插件数据
        :param plugin_id: 插件id
        :param key: 数据key
        """
        if key:
            data = self._execute_sync_query(
                lambda session: PluginData.get_plugin_data_by_key(
                    session, plugin_id, key
                )
            )
            if not data:
                return None
            return data.value
        else:
            return self._execute_sync_query(
                lambda session: PluginData.get_plugin_data(session, plugin_id)
            )

    async def async_get_data(self, plugin_id: str, key: Optional[str] = None) -> Any:
        """
        异步获取插件数据。
        :param plugin_id: 插件id
        :param key: 数据key
        """
        if key:
            data = await self._execute_async_query(
                lambda session: PluginData.async_get_plugin_data_by_key(
                    session, plugin_id, key
                )
            )
            if not data:
                return None
            return data.value
        return await self._execute_async_query(
            lambda session: PluginData.async_get_plugin_data(session, plugin_id)
        )

    def del_data(self, plugin_id: str, key: Optional[str] = None) -> Any:
        """
        删除插件数据
        :param plugin_id: 插件id
        :param key: 数据key
        """
        def stage(session: Session) -> None:
            """把删除入口映射到调用方或组合根持有的事务。"""
            if key:
                PluginData.del_plugin_data_by_key(session, plugin_id, key)
            else:
                PluginData.del_plugin_data(session, plugin_id)

        self._execute_sync_write(stage)

    def stage_delete(self, plugin_id: str) -> None:
        """暂存目标插件全部数据删除并 flush，不提交调用方事务。"""
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

    def get_data_all(self, plugin_id: str) -> Any:
        """
        获取插件所有数据
        :param plugin_id: 插件id
        """
        return self._execute_sync_query(
            lambda session: PluginData.get_plugin_data_by_plugin_id(
                session, plugin_id
            )
        )

    async def async_get_data_all(self, plugin_id: str) -> Any:
        """
        异步获取插件所有数据。
        :param plugin_id: 插件id
        """
        return await self._execute_async_query(
            lambda session: PluginData.async_get_plugin_data_by_plugin_id(
                session, plugin_id
            )
        )
