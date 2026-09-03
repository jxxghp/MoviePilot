"""插件实例描述符的数据访问原语。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, cast

from sqlalchemy import select, update
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

    def set_default_target(self, source_plugin_id: str, instance_id: str) -> bool:
        """原子地把某源插件的默认调用目标改为指定实例，同一事务内清旧置新。

        目标行须已经落盘——调用方须先确保待置位的本体或分身描述符已经存在，
        这里只按 ``instance_id`` 与 ``source_plugin_id`` 双重匹配定位目标行，不做
        隐式创建；命中失败原样返回，不动同插件原有的置位。命中时先清后置，
        两条 DML 处在同一 session、同一事务内提交，中途不会出现两行同时为真；
        并发写入下的唯一性最终由表上的条件唯一索引兜底。

        :param source_plugin_id: 源插件 ID
        :param instance_id: 要设为默认调用目标的实例 ID
        :return: 目标行存在并已置位为 True，目标行不存在时为 False
        """
        def stage(session: Session) -> bool:
            """在同一事务内定位目标行、清除同插件其余置位、置位目标行。"""
            target = session.execute(
                select(PluginInstanceDescriptor).where(
                    PluginInstanceDescriptor.instance_id == instance_id,
                    PluginInstanceDescriptor.source_plugin_id == source_plugin_id,
                )
            ).scalars().first()
            if target is None:
                return False
            session.execute(
                update(PluginInstanceDescriptor)
                .where(
                    PluginInstanceDescriptor.source_plugin_id == source_plugin_id,
                    PluginInstanceDescriptor.instance_id != instance_id,
                    PluginInstanceDescriptor.is_default_target.is_(True),
                )
                .values(is_default_target=False)
            )
            target.is_default_target = True
            session.add(target)
            return True

        return bool(self._execute_sync_write(stage))

    def clear_default_target(self, source_plugin_id: str) -> None:
        """清除某源插件的默认调用目标置位，重复调用保持幂等。"""
        def stage(session: Session) -> None:
            """在同一事务内清除该源插件全部置位的行。"""
            session.execute(
                update(PluginInstanceDescriptor)
                .where(
                    PluginInstanceDescriptor.source_plugin_id == source_plugin_id,
                    PluginInstanceDescriptor.is_default_target.is_(True),
                )
                .values(is_default_target=False)
            )

        self._execute_sync_write(stage)
