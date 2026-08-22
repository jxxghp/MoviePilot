"""服务实例配置数据访问。"""
from typing import Any, Iterable, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.serviceconfig import BUILTIN_PROVIDER, ServiceConfig


class ServiceConfigNameConflictError(Exception):
    """同族同类型下已有同名实例配置，实例名必须唯一。"""


class ServiceConfigOper(DbOper):
    """封装服务实例配置的查询、增删改与默认调用目标裁决。

    唯一约束冲突在本层被翻译成领域异常：``(capability, type, name)`` 的唯一性是
    用户在界面上能理解的规则（同一类型下不能有两个同名实例），把 ``IntegrityError``
    原样抛到界面既暴露表结构，也没法告诉用户该改什么。
    """

    @staticmethod
    def _conflict_message(capability: str, service_type: str, name: str) -> str:
        """
        构造实例名冲突的用户可读提示。
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 提示文案
        """
        return f"{capability} 类型 {service_type} 下已存在名为 {name} 的配置，请换一个名称"

    def list_by_capability(self, capability: str) -> List[ServiceConfig]:
        """
        列出某族的全部实例配置。
        :param capability: 族标识
        :return: 该族全部实例配置行
        """
        return ServiceConfig.list_by_capability(self._db, capability)

    def list_by_type(self, capability: str, service_type: str) -> List[ServiceConfig]:
        """
        列出某族某类型的全部实例配置。
        :param capability: 族标识
        :param service_type: 类型标识
        :return: 该类型全部实例配置行
        """
        return ServiceConfig.list_by_type(self._db, capability, service_type)

    @staticmethod
    def to_payload(record: ServiceConfig) -> dict:
        """
        把一行实例配置摊平成该族配置模型接受的形状。

        宿主载荷先铺开、身份字段后覆盖：``host_config`` 是用户可写的 JSON，混进
        ``name``/``type`` 这类键时不能顶掉行本身的身份，否则一行配置读出来会变成另一个实例。
        :param record: 实例配置行
        :return: 与该族配置模型字段对应的配置字典
        """
        payload = dict(record.host_config or {})
        payload.update({
            "name": record.name,
            "type": record.type,
            "enabled": bool(record.enabled),
            "config": record.config or {},
            "default": bool(record.is_default_target),
        })
        return payload

    def list_payloads(self, capability: str) -> List[dict]:
        """
        列出某族全部实例配置的摊平形状，按写入先后排列。
        :param capability: 族标识
        :return: 该族全部配置字典
        """
        return [self.to_payload(record) for record in self.list_by_capability(capability)]

    @staticmethod
    def to_row(record: ServiceConfig) -> dict:
        """
        把一行实例配置摊平成列名到列值的字典。

        与 `to_payload` 的差别在于视角：摊平形状是「该族配置模型接受的样子」，供扇出
        实例与整族读写使用，宿主载荷已铺开在顶层且不含 ``capability``/``provider``；
        本形状是「表里那一行的样子」，逐列对应、不铺开也不改名，供逐条读写与「提供方
        已消失」这类按列判定的读取方使用。
        :param record: 实例配置行
        :return: 列名到列值的字典
        """
        return {
            "capability": record.capability,
            "type": record.type,
            "name": record.name,
            "enabled": bool(record.enabled),
            "config": record.config or {},
            "host_config": record.host_config or {},
            "is_default_target": bool(record.is_default_target),
            "provider": record.provider,
        }

    def list_rows(self, capability: str) -> List[dict]:
        """
        列出某族全部实例配置的行形状，按写入先后排列。
        :param capability: 族标识
        :return: 该族全部配置行的列名到列值字典
        """
        return [self.to_row(record) for record in self.list_by_capability(capability)]

    def list_rows_by_type(self, capability: str, service_type: str) -> List[dict]:
        """
        列出某族某类型全部实例配置的行形状，按写入先后排列。
        :param capability: 族标识
        :param service_type: 类型标识
        :return: 该类型全部配置行的列名到列值字典
        """
        return [self.to_row(record) for record in self.list_by_type(capability, service_type)]

    def get_row(self, capability: str, service_type: str, name: str) -> Optional[dict]:
        """
        按 ``(capability, type, name)`` 取单条实例配置的行形状。
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 命中的配置行字典，不存在返回 None
        """
        record = self.get(capability, service_type, name)
        return self.to_row(record) if record is not None else None

    def replace_capability(self, capability: str, records: List[dict]) -> int:
        """
        用给定的整族配置覆盖某族现有配置。
        :param capability: 族标识
        :param records: 该族的全部配置行
        :return: 覆盖后该族的配置行数
        """
        return ServiceConfig.replace_capability(self._db, capability, records)

    def get(self, capability: str, service_type: str, name: str) -> Optional[ServiceConfig]:
        """
        按 ``(capability, type, name)`` 精确取单条实例配置。
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 命中的配置行，不存在返回 None
        """
        return ServiceConfig.get_by_identity(self._db, capability, service_type, name)

    def list_by_provider(self, provider: str) -> List[ServiceConfig]:
        """
        列出某提供方名下的全部实例配置。
        :param provider: 提供方标识
        :return: 该提供方名下的实例配置行
        """
        return ServiceConfig.list_by_provider(self._db, provider)

    def list_with_absent_provider(
            self, present_providers: Iterable[str]
    ) -> List[ServiceConfig]:
        """
        列出提供方已不在场的实例配置，供界面提示「该类型由扩展 X 提供，X 当前未启用」。

        入参是当前在场的提供方全集，由调用方从运行态取得；内建保留值恒视为在场。
        :param present_providers: 当前在场的提供方标识集合
        :return: 提供方已消失的实例配置行
        """
        return ServiceConfig.list_with_absent_provider(self._db, present_providers)

    def add(
            self,
            capability: str,
            service_type: str,
            name: str,
            *,
            config: Optional[Any] = None,
            host_config: Optional[Any] = None,
            enabled: bool = False,
            provider: str = BUILTIN_PROVIDER,
    ) -> ServiceConfig:
        """
        新增一条实例配置。

        新增的实例一律不是默认调用目标，默认调用目标必须由用户显式选定。
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :param config: 类型专属配置载荷
        :param host_config: 宿主消费的实例级字段载荷
        :param enabled: 是否启用
        :param provider: 提供该类型的扩展标识，内建取 ``BUILTIN_PROVIDER``
        :return: 新增的配置行
        :raises ServiceConfigNameConflictError: 同族同类型下已有同名配置
        """
        if self.get(capability, service_type, name) is not None:
            raise ServiceConfigNameConflictError(
                self._conflict_message(capability, service_type, name)
            )
        record = ServiceConfig(
            capability=capability,
            type=service_type,
            name=name,
            enabled=enabled,
            config=config,
            host_config=host_config,
            is_default_target=False,
            provider=provider,
        )
        def stage(session: Session) -> None:
            """在调用方事务中暂存实例配置并立即 flush，使唯一约束冲突在本方法内可见。"""
            session.add(record)
            session.flush()

        try:
            self._execute_sync_write(stage)
        except IntegrityError as error:
            # 预检查与写入之间另一请求刚好写入了同名配置，由唯一约束兜底
            raise ServiceConfigNameConflictError(
                self._conflict_message(capability, service_type, name)
            ) from error
        # flush 已分配主键并把字段写入当前事务，直接返回即可；是否提交由调用方
        # （无显式会话时是本方法委托的兼容事务）决定，不在这里替调用方提前收尾。
        return record

    def add_row(self, capability: str, record: dict) -> dict:
        """
        按配置行新增一条实例配置。

        入参是 `to_row` 的形状，供逐条写入口直接把整形好的行交进来；``is_default_target``
        不从行里取，新增的实例一律不是默认调用目标。
        :param capability: 族标识
        :param record: 配置行，含 type/name/enabled/config/host_config/provider
        :return: 新增配置行的列名到列值字典
        :raises ServiceConfigNameConflictError: 同族同类型下已有同名配置
        """
        return self.to_row(self.add(
            capability,
            record["type"],
            record["name"],
            config=record.get("config"),
            host_config=record.get("host_config"),
            enabled=bool(record.get("enabled")),
            provider=record.get("provider") or BUILTIN_PROVIDER,
        ))

    def update(
            self, capability: str, service_type: str, name: str, payload: dict
    ) -> bool:
        """
        更新单条实例配置，可写列见 ``UPDATABLE_FIELDS``。

        改名同样走这里，因此可能撞上唯一约束；启用态与配置载荷各自独立更新，
        不必把整族配置读出来改回去。
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :param payload: 待写入的列值
        :return: 是否更新了配置行
        :raises ServiceConfigNameConflictError: 改名后与同族同类型下的既有配置重名
        """
        new_name = payload.get("name")
        if new_name is not None and new_name != name:
            if self.get(capability, service_type, new_name) is not None:
                raise ServiceConfigNameConflictError(
                    self._conflict_message(capability, service_type, new_name)
                )
        try:
            updated = ServiceConfig.update_by_identity(
                self._db, capability, service_type, name, payload
            )
        except IntegrityError as error:
            raise ServiceConfigNameConflictError(
                self._conflict_message(capability, service_type, new_name or name)
            ) from error
        return bool(updated)

    def delete(self, capability: str, service_type: str, name: str) -> bool:
        """
        删除单条实例配置。
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 是否删除了配置行
        """
        return bool(
            ServiceConfig.delete_by_identity(self._db, capability, service_type, name)
        )

    def get_default_target(self, capability: str) -> Optional[ServiceConfig]:
        """
        取某族的默认调用目标实例。
        :param capability: 族标识
        :return: 置位的配置行，该族未设置默认调用目标时返回 None
        """
        return ServiceConfig.get_default_target(self._db, capability)

    def set_default_target(self, capability: str, service_type: str, name: str) -> bool:
        """
        把某族的默认调用目标改为指定实例，目标不存在时不动原有置位。
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 是否完成置位
        """
        return bool(
            ServiceConfig.set_default_target(self._db, capability, service_type, name)
        )

    def clear_default_target(self, capability: str) -> int:
        """
        清除某族的默认调用目标置位。
        :param capability: 族标识
        :return: 清除的行数
        """
        return ServiceConfig.clear_default_target(self._db, capability)
