"""插件实例描述符的数据访问原语。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.plugininstance import PluginInstanceDescriptor


class PluginInstanceOper(DbOper):
    """在调用方 Session 或独占事务中查询并暂存插件实例描述符。"""

    def get(self, instance_id: str) -> Optional[PluginInstanceDescriptor]:
        """按实例 ID 查询单条描述符，分身与本体共用同一张表和同一个查询。"""
        return self._execute_sync_query(
            lambda session: cast(
                Optional[PluginInstanceDescriptor],
                session.execute(
                    select(PluginInstanceDescriptor).where(
                        PluginInstanceDescriptor.instance_id == instance_id
                    )
                ).scalars().first(),
            )
        )

    def list_by_source(self, source_plugin_id: str) -> list[PluginInstanceDescriptor]:
        """按源插件 ID 列举其全部实例描述符，含分身与本体。"""
        return list(
            self._execute_sync_query(
                lambda session: session.execute(
                    select(PluginInstanceDescriptor).where(
                        PluginInstanceDescriptor.source_plugin_id == source_plugin_id
                    )
                ).scalars()
            )
        )

    def list_all(self) -> list[PluginInstanceDescriptor]:
        """列举全部实例描述符，供运行期批量装载和兜底导入判空使用。"""
        return list(
            self._execute_sync_query(
                lambda session: session.execute(
                    select(PluginInstanceDescriptor)
                ).scalars()
            )
        )

    def save(self, **fields: Any) -> PluginInstanceDescriptor:
        """按 ``instance_id`` 新增或更新一条描述符。

        :param fields: 描述符字段，须含 ``instance_id``
        :return: 写入后的描述符
        """
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get(fields["instance_id"])
        if existing is not None:
            return self._stage_update(existing, {**fields, "updated_at": now})
        return self._stage_create(
            PluginInstanceDescriptor(**fields, created_at=now, updated_at=now)
        )

    def delete(self, instance_id: str) -> bool:
        """按实例 ID 删除描述符，返回删除前是否存在。"""
        def stage(session: Session) -> bool:
            """在同一事务内查询并删除，避免读写跨两个独立事务。"""
            existing = session.execute(
                select(PluginInstanceDescriptor).where(
                    PluginInstanceDescriptor.instance_id == instance_id
                )
            ).scalars().first()
            if existing is None:
                return False
            session.delete(existing)
            return True

        return bool(self._execute_sync_write(stage))
