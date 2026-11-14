"""
插件持久化数据的数据访问入口，适配调用方会话及兼容事务执行器。
"""

from typing import Any, Optional, Union

from sqlalchemy import Executable, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.plugindata import PluginData


class PluginDataOper(DbOper):
    """
    插件数据管理
    """

    def save(self, plugin_id: str, key: str, value: Any) -> None:
        """
        保存插件数据

        :param plugin_id: 插件id
        :param key: 数据key
        :param value: 数据值
        """
        def stage(session: Session) -> None:
            """
            在调用方事务内原子写入并刷新已加载的同键模型。
            """
            session.execute(
                self._save_statement(session, plugin_id, key, value),
                execution_options={"populate_existing": True},
            ).all()

        self._execute_sync_write(stage)

    async def async_save(self, plugin_id: str, key: str, value: Any) -> None:
        """
        在调用方异步事务中原子保存插件同键数据。

        :param plugin_id: 插件标识
        :param key: 数据键
        :param value: 可JSON序列化的数据值
        """
        async def stage(session: AsyncSession) -> None:
            """
            使用唯一键upsert，避免并发首次保存各自插入新行。
            """
            result = await session.execute(
                self._save_statement(session, plugin_id, key, value),
                execution_options={"populate_existing": True},
            )
            result.all()

        await self._execute_async_write(stage)

    @staticmethod
    def _save_statement(
        session: Union[Session, AsyncSession], plugin_id: str, key: str, value: Any,
    ) -> Executable:
        """
        为受支持的SQLite或PostgreSQL生成同一唯一键的原子写入语句。
        """
        insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        statement = insert(PluginData).values(plugin_id=plugin_id, key=key, value=value)
        return statement.on_conflict_do_update(
            index_elements=[PluginData.plugin_id, PluginData.key],
            set_={"value": statement.excluded.value},
        ).returning(PluginData)

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
            """
            把删除入口映射到调用方或组合根持有的事务。
            """
            if key:
                PluginData.del_plugin_data_by_key(session, plugin_id, key)
            else:
                PluginData.del_plugin_data(session, plugin_id)

        self._execute_sync_write(stage)

    def stage_delete(self, plugin_id: str) -> None:
        """
        暂存目标插件全部数据删除并 flush，不提交调用方事务。
        """
        if not isinstance(self._db, Session):
            raise RuntimeError("插件数据暂存删除需要调用方提供 Session")
        self._db.execute(
            delete(PluginData).where(PluginData.plugin_id == plugin_id)
        )
        self._db.flush()

    def truncate(self) -> None:
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
