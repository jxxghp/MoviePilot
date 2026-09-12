"""插件实例的数据访问原语。"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Optional, cast

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.plugininstance import PluginInstance


def _now() -> str:
    """返回实例行使用的 ISO-8601 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


class PluginInstanceOper(DbOper):
    """在调用方 Session 或独占事务中查询并暂存插件实例。

    一行由 ``instance_id`` 唯一确定，``source_plugin_id`` 指向提供代码的源插件；
    两者相等即源插件本体，不等即共享其源码的分身。
    """

    def get(self, instance_id: str) -> Optional[PluginInstance]:
        """按实例 ID 查询单行，分身与本体共用同一张表和同一个查询。

        停用的实例照常返回：它仍是一份登记在册的配置，只是不该被实例化。是否据此
        装载由读取方按 ``is_enabled`` 判断，而不是在这里就把它藏起来。
        """
        return self._execute_sync_query(
            lambda session: PluginInstance.get_by_instance_id(session, instance_id)
        )

    def list_by_source(self, source_plugin_id: str) -> list[PluginInstance]:
        """按源插件 ID 列举其全部实例，含分身与本体、启用与停用。"""
        return list(
            self._execute_sync_query(
                lambda session: PluginInstance.list_by_source_plugin_id(
                    session, source_plugin_id
                )
            )
            or []
        )

    def list_all(self) -> list[PluginInstance]:
        """列举全部实例，含停用的，供目录投影与兜底导入判空使用。"""
        return list(
            self._execute_sync_query(
                lambda session: session.execute(select(PluginInstance)).scalars().all()
            )
        )

    def list_enabled(self) -> list[PluginInstance]:
        """列举应当被实例化并启动的配置，供运行期批量装载使用。"""
        return list(
            self._execute_sync_query(
                lambda session: session.execute(
                    select(PluginInstance).where(PluginInstance.is_enabled.is_(True))
                ).scalars().all()
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

        return cast(PluginInstance, self._execute_sync_write(stage))

    def delete(self, instance_id: str) -> bool:
        """按实例 ID 删除整行，返回删除前是否存在。

        这会连同该实例的配置、日志等级覆盖与锚定版本一起清除，属于彻底清理；
        只想停掉实例、保留设置用 :meth:`set_enabled` 置假。
        """
        def stage(session: Session) -> bool:
            """在同一事务内查询并删除，避免读写跨两个独立事务。"""
            existing = PluginInstance.get_by_instance_id(session, instance_id)
            if existing is None:
                return False
            session.delete(existing)
            return True

        return bool(self._execute_sync_write(stage))

    def save_config_data(
        self,
        *,
        instance_id: str,
        source_plugin_id: str,
        config_data: Any,
    ) -> Optional[bool]:
        """写入业务参数，保留该行已有的身份、锚定版本、启用位与日志等级。

        :return: True 已写入，None 值未变化无需写入
        """

        def stage(session: Session) -> Optional[bool]:
            """在同一事务内创建或更新业务参数。"""
            record = PluginInstance.get_by_instance_id(session, instance_id)
            if record is None:
                session.add(
                    PluginInstance(
                        instance_id=instance_id,
                        source_plugin_id=source_plugin_id,
                        config_data=copy.deepcopy(config_data),
                        is_enabled=True,
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )
                return True
            if record.config_data == config_data:
                return None
            record.config_data = copy.deepcopy(config_data)
            record.updated_at = _now()
            return True

        return self._execute_sync_write(stage)

    def clear_config_data(self, instance_id: str) -> bool:
        """清空某实例的业务参数，保留其身份与其余设置。

        清空后若该行是本体且各列皆空，整行一并移除，避免只剩一对身份列的空行堆积。

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

    def set_log_level(
        self,
        *,
        instance_id: str,
        log_level: Optional[str],
        log_expires_at: Optional[str],
    ) -> bool:
        """写入日志等级覆盖，没有实例行时按本体建出。

        只设过等级、没存过参数的插件同样要有一行，否则覆盖无处落盘。
        """

        def stage(session: Session) -> bool:
            """在同一事务内写入等级覆盖。"""
            record = PluginInstance.get_by_instance_id(session, instance_id)
            if record is None:
                if log_level is None and log_expires_at is None:
                    return False
                session.add(
                    PluginInstance(
                        instance_id=instance_id,
                        source_plugin_id=instance_id,
                        log_level=log_level,
                        log_expires_at=log_expires_at,
                        is_enabled=True,
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )
                return True
            record.log_level = log_level
            record.log_expires_at = log_expires_at
            record.updated_at = _now()
            return True

        return bool(self._execute_sync_write(stage))

    def set_enabled(self, *, instance_id: str, is_enabled: bool) -> bool:
        """写入启用位，它同时就是卸载与恢复的开关。

        置假即卸载：业务参数、展示信息与锚定版本原样留在这一行等待再次启用，因而
        不需要另设一个卸载时间列。同时清掉两项只对启用中的实例才有意义的状态——
        默认调用目标置位，否则未指定实例的外部调用会被路由到一个不会被实例化的
        实例；日志等级覆盖，它带失效时间、本就是临时调试设置，而运行期在停用时会
        同步清掉进程内的等级覆盖表，库里留着只会让两边不一致。

        它与该实例此刻是否在运行无关——运行态由运行时持有、不落盘；也与插件自身
        在 ``config_data`` 里按场景判定的业务开关无关。
        """

        def stage(session: Session) -> bool:
            """在同一事务内写入启用位，停用时一并清掉仅对启用实例有意义的状态。"""
            record = PluginInstance.get_by_instance_id(session, instance_id)
            if record is None:
                return False
            record.is_enabled = is_enabled
            if not is_enabled:
                record.is_default_target = False
                record.log_level = None
                record.log_expires_at = None
            record.updated_at = _now()
            return True

        return bool(self._execute_sync_write(stage))

    def set_default_target(self, source_plugin_id: str, instance_id: str) -> bool:
        """原子地把某源插件的默认调用目标改为指定实例，同一事务内清旧置新。

        目标行须已经落盘——调用方须先确保待置位的本体或分身实例行已经存在，
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
                select(PluginInstance).where(
                    PluginInstance.instance_id == instance_id,
                    PluginInstance.source_plugin_id == source_plugin_id,
                )
            ).scalars().first()
            if target is None:
                return False
            session.execute(
                update(PluginInstance)
                .where(
                    PluginInstance.source_plugin_id == source_plugin_id,
                    PluginInstance.instance_id != instance_id,
                    PluginInstance.is_default_target.is_(True),
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
                update(PluginInstance)
                .where(
                    PluginInstance.source_plugin_id == source_plugin_id,
                    PluginInstance.is_default_target.is_(True),
                )
                .values(is_default_target=False)
            )

        self._execute_sync_write(stage)
