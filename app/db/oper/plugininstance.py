"""插件实例的数据访问原语。"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Optional, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.plugininstance import PluginInstance


def _now() -> str:
    """返回实例行使用的 ISO-8601 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def _apply_config_data(
    session: Union[Session, AsyncSession],
    record: Optional[PluginInstance],
    *,
    instance_id: str,
    source_plugin_id: str,
    config_data: Any,
) -> Optional[bool]:
    """把业务参数写进已查出的行，行不存在则按本次身份建出一行。

    同步与异步写入只有查询方式不同，落库决策共用这一份：两边各写一份会让
    「值未变化不写」和「建行用哪个源插件」在两条路径上悄悄分叉。

    :param session: 调用方事务所持的会话，同步与异步的 ``add`` 都是同步方法
    :param record: 已查出的实例行，None 表示该实例尚无行
    :param instance_id: 实例 ID
    :param source_plugin_id: 该行不存在时用于建行的源插件 ID
    :param config_data: 业务参数
    :return: True 已写入，None 值未变化无需写入
    """
    if record is None:
        now = _now()
        session.add(
            PluginInstance(
                instance_id=instance_id,
                source_plugin_id=source_plugin_id,
                config_data=copy.deepcopy(config_data),
                created_at=now,
                updated_at=now,
            )
        )
        return True
    if record.config_data == config_data:
        return None
    record.config_data = copy.deepcopy(config_data)
    record.updated_at = _now()
    return True


class PluginInstanceOper(DbOper):
    """在调用方 Session 或独占事务中查询并暂存插件实例。

    一行由 ``instance_id`` 唯一确定，``source_plugin_id`` 指向提供代码的源插件；
    两者相等即源插件本体，不等即共享其源码的分身。
    """

    def get(self, instance_id: str) -> Optional[PluginInstance]:
        """按实例 ID 查询单行，分身与本体共用同一张表和同一个查询。"""
        return self._execute_sync_query(
            lambda session: PluginInstance.get_by_instance_id(session, instance_id)
        )

    def list_by_source(self, source_plugin_id: str) -> list[PluginInstance]:
        """按源插件 ID 列举其全部实例，含分身与本体。"""
        return list(
            self._execute_sync_query(
                lambda session: PluginInstance.list_by_source_plugin_id(
                    session, source_plugin_id
                )
            )
            or []
        )

    def list_all(self) -> list[PluginInstance]:
        """列举全部实例，供目录投影与兜底导入判空使用。"""
        return list(
            self._execute_sync_query(
                lambda session: session.execute(select(PluginInstance)).scalars().all()
            )
        )

    def save(self, **fields: Any) -> PluginInstance:
        """按 ``instance_id`` 新增或更新一行，只写入本次给出的列。

        查询与写入收在同一事务内，避免两个并发的首次写入各自读到空、双双插入而
        撞上 ``instance_id`` 唯一键。

        已存在的行不允许改写 ``source_plugin_id``：它连同 ``instance_id`` 构成这一行
        的身份，改写会把整行的归属换掉，同时把本体与分身的角色判定一并翻转，只可能
        来自调用方错把分身自身实例 ID 当作源插件 ID 使用，因而在持久化层直接拒绝，
        不依赖上层纪律。

        :param fields: 实例字段，须含 ``instance_id``
        :return: 写入后的实例行
        :raise ValueError: 已存在的行与本次写入的 ``source_plugin_id`` 不一致
        """
        now = _now()
        instance_id = fields["instance_id"]

        def stage(session: Session) -> PluginInstance:
            """在同一事务内查询并新增或更新实例行。"""
            existing = PluginInstance.get_by_instance_id(session, instance_id)
            if existing is None:
                record = PluginInstance(**fields, created_at=now, updated_at=now)
                session.add(record)
                return record
            incoming_source = fields.get("source_plugin_id")
            if incoming_source is not None and existing.source_plugin_id != incoming_source:
                raise ValueError(
                    f"插件实例 {instance_id} 已归属于 {existing.source_plugin_id}，"
                    f"不能改写为 {incoming_source}"
                )
            for key, value in {**fields, "updated_at": now}.items():
                setattr(existing, key, value)
            return existing

        return self._execute_sync_write(stage)

    def delete(self, instance_id: str) -> bool:
        """按实例 ID 删除整行，返回删除前是否存在。

        这会连同该实例的业务参数与展示信息一起清除：分身的存在本身就由这一行表达，
        删行即卸载分身。
        """

        def stage(session: Session) -> bool:
            """在同一事务内查询并删除，避免读写跨两个独立事务。"""
            existing = PluginInstance.get_by_instance_id(session, instance_id)
            if existing is None:
                return False
            session.delete(existing)
            return True

        return bool(self._execute_sync_write(stage))

    def get_config_data(self, instance_id: str) -> Any:
        """读取某实例的业务参数；该行不存在或从未存过参数都返回 None。

        返回深拷贝：调用方拿到的是一份独立副本，就地改动不会顺着 ORM 行漏进
        下一次写入的「值未变化」判定。

        :param instance_id: 实例 ID
        :return: 业务参数，无则 None
        """
        record = self.get(instance_id)
        return copy.deepcopy(record.config_data) if record is not None else None

    def save_config_data(
        self,
        *,
        instance_id: str,
        source_plugin_id: str,
        config_data: Any,
    ) -> Optional[bool]:
        """写入业务参数，保留该行已有的身份与展示信息。

        :param instance_id: 实例 ID
        :param source_plugin_id: 该行不存在时用于建行的源插件 ID
        :param config_data: 业务参数
        :return: True 已写入，None 值未变化无需写入
        """

        def stage(session: Session) -> Optional[bool]:
            """在同一事务内创建或更新业务参数。"""
            return _apply_config_data(
                session,
                PluginInstance.get_by_instance_id(session, instance_id),
                instance_id=instance_id,
                source_plugin_id=source_plugin_id,
                config_data=config_data,
            )

        return self._execute_sync_write(stage)

    async def async_save_config_data(
        self,
        *,
        instance_id: str,
        source_plugin_id: str,
        config_data: Any,
    ) -> Optional[bool]:
        """在异步事务中写入业务参数，行为与同步写入一致。

        :param instance_id: 实例 ID
        :param source_plugin_id: 该行不存在时用于建行的源插件 ID
        :param config_data: 业务参数
        :return: True 已写入，None 值未变化无需写入
        """

        async def stage(session: AsyncSession) -> Optional[bool]:
            """在同一异步事务内创建或更新业务参数。"""
            return _apply_config_data(
                session,
                await PluginInstance.async_get_by_instance_id(session, instance_id),
                instance_id=instance_id,
                source_plugin_id=source_plugin_id,
                config_data=config_data,
            )

        return await self._execute_async_write(stage)

    def set_log_level(
        self,
        *,
        instance_id: str,
        log_level: Optional[str],
        log_expires_at: Optional[str],
    ) -> bool:
        """写入或清除该实例的日志等级覆盖，没有实例行时按本体建出。

        只设过等级、从没存过参数的插件同样要有一行，否则覆盖无处落盘；反过来，
        清除覆盖后若该行是本体且各列皆空，整行一并回收，与清空业务参数同一规则，
        不让只剩一对身份列的空行堆积。

        :param instance_id: 实例 ID
        :param log_level: 目标等级名，None 表示清除覆盖
        :param log_expires_at: 覆盖失效时间的 ISO-8601 文本，None 表示不过期
        :return: 是否发生了写入；清除一个本来就没有覆盖的实例返回 False
        """

        def stage(session: Session) -> bool:
            """在同一事务内写入等级覆盖，并回收清空后只剩身份的本体行。"""
            record = PluginInstance.get_by_instance_id(session, instance_id)
            if record is None:
                if log_level is None and log_expires_at is None:
                    return False
                now = _now()
                session.add(
                    PluginInstance(
                        instance_id=instance_id,
                        source_plugin_id=instance_id,
                        log_level=log_level,
                        log_expires_at=log_expires_at,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return True
            record.log_level = log_level
            record.log_expires_at = log_expires_at
            record.updated_at = _now()
            if record.is_host and record.carries_only_identity:
                session.delete(record)
            return True

        return bool(self._execute_sync_write(stage))

    def clear_config_data(self, instance_id: str) -> bool:
        """清空某实例的业务参数，保留其身份与展示信息。

        清空后若该行是本体且各列皆空，整行一并移除，避免只剩一对身份列的空行堆积。

        :param instance_id: 实例 ID
        :return: 该行存在并已处理
        """

        def stage(session: Session) -> bool:
            """在同一事务内清空业务参数并回收空的本体行。"""
            record = PluginInstance.get_by_instance_id(session, instance_id)
            if record is None:
                return False
            record.config_data = None
            record.updated_at = _now()
            if record.is_host and record.carries_only_identity:
                session.delete(record)
            return True

        return bool(self._execute_sync_write(stage))
