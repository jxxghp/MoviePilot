"""插件安装事务 Application Port 的同步 SQLAlchemy 实现。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.transaction import (
    PluginInstallationConflictError,
    PluginInstallationPhase,
    PluginInstallationRecord,
    PluginInstallationStore,
)
from app.db.models.pluginidentity import PluginIdentity as IdentityModel
from app.db.models.plugininstallation import PluginInstallation

_INSTALLED_PLUGINS_KEY = "UserInstalledPlugins"
AtomicMembershipUpdater = Callable[
    [
        str,
        Callable[
            [Session, object],
            tuple[PluginInstallationRecord, list[str]],
        ],
    ],
    PluginInstallationRecord,
]


def _identity_from_model(model: IdentityModel) -> PluginIdentity:
    """把同一 Session 读出的身份模型还原为应用记录。"""
    return PluginIdentity(
        plugin_id=model.plugin_id,
        normalized_plugin_id=model.normalized_plugin_id,
        trusted_source_type=TrustedPluginSourceType(model.trusted_source_type),
        trusted_source_key=model.trusted_source_key,
        binding_basis=PluginBindingBasis(model.binding_basis),
        payload_source_type=PluginPayloadSourceType(model.payload_source_type),
        payload_source_key=model.payload_source_key,
        declared_version=model.declared_version,
        package_generation=model.package_generation,
        system_version=model.system_version,
        supports_v3=model.supports_v3,
        supports_v3t=model.supports_v3t,
        payload_receipt=model.payload_receipt,
        revision=model.revision,
        created_at=datetime.fromisoformat(model.created_at),
        updated_at=datetime.fromisoformat(model.updated_at),
        bound_at=(
            datetime.fromisoformat(model.bound_at)
            if model.bound_at
            else None
        ),
        payload_applied_at=(
            datetime.fromisoformat(model.payload_applied_at)
            if model.payload_applied_at
            else None
        ),
    )


def _identity_model_values(identity: PluginIdentity) -> dict[str, object]:
    """把应用身份映射为不含自增主键的模型列值。"""
    return {
        "plugin_id": identity.plugin_id,
        "normalized_plugin_id": identity.normalized_plugin_id,
        "trusted_source_type": identity.trusted_source_type.value,
        "trusted_source_key": identity.trusted_source_key,
        "binding_basis": identity.binding_basis.value,
        "payload_source_type": identity.payload_source_type.value,
        "payload_source_key": identity.payload_source_key,
        "declared_version": identity.declared_version,
        "package_generation": identity.package_generation,
        "system_version": identity.system_version,
        "supports_v3": identity.supports_v3,
        "supports_v3t": identity.supports_v3t,
        "payload_receipt": identity.payload_receipt,
        "revision": identity.revision,
        "created_at": identity.created_at.isoformat(),
        "updated_at": identity.updated_at.isoformat(),
        "bound_at": identity.bound_at.isoformat() if identity.bound_at else None,
        "payload_applied_at": (
            identity.payload_applied_at.isoformat()
            if identity.payload_applied_at
            else None
        ),
    }


class TransactionalPluginInstallationStore(PluginInstallationStore):
    """以单张事务表协调单插件 membership、来源身份和 phase。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        update_membership_atomically: AtomicMembershipUpdater,
    ) -> None:
        """保存事务会话工厂和配置 membership 的窄原子写入口。"""
        self._session_factory = session_factory
        self.__update_membership_atomically = update_membership_atomically

    def __session(self) -> Session:
        """创建不会在提交后过期状态的短生命周期 Session。"""
        session = self._session_factory()
        session.expire_on_commit = False
        return session

    @staticmethod
    def __now() -> str:
        """生成带时区的持久化更新时间。"""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def __phase(value: PluginInstallationPhase | str) -> PluginInstallationPhase:
        """把调用方 phase 转为受限枚举。"""
        try:
            return (
                value
                if isinstance(value, PluginInstallationPhase)
                else PluginInstallationPhase(value)
            )
        except ValueError as error:
            raise PluginInstallationConflictError(
                f"未知插件安装 phase: {value}"
            ) from error

    @staticmethod
    def __to_record(model: PluginInstallation) -> PluginInstallationRecord:
        """把 ORM 行还原为经过应用层校验的事务记录。"""
        try:
            return PluginInstallationRecord(
                transaction_id=model.transaction_id,
                plugin_id=model.plugin_id,
                phase=PluginInstallationPhase(model.phase),
                membership_before=model.membership_before,
                membership_target=model.membership_target,
                identity_before_revision=model.identity_before_revision,
                identity_target_revision=model.identity_target_revision,
                package_existed=model.package_existed,
                persistent_backup_existed=model.persistent_backup_existed,
                created_at=datetime.fromisoformat(model.created_at),
                updated_at=datetime.fromisoformat(model.updated_at),
                schema_version=model.schema_version,
            )
        except (TypeError, ValueError) as error:
            raise PluginInstallationConflictError(
                f"插件安装事务 {model.transaction_id} 的持久化状态无效"
            ) from error

    @staticmethod
    def __identity_query(session: Session, plugin_id: str) -> IdentityModel | None:
        """读取并锁定指定插件的身份行。"""
        return session.execute(
            select(IdentityModel)
            .where(IdentityModel.normalized_plugin_id == plugin_id.lower())
            .with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def __membership_state(current: object, plugin_id: str) -> bool:
        """只读取目标插件 membership，不把其他插件写入事务快照。"""
        if current is None:
            return False
        if not isinstance(current, list) or any(
            not isinstance(item, str) for item in current
        ):
            raise PluginInstallationConflictError(
                "UserInstalledPlugins 当前值不是 JSON 字符串数组"
            )
        normalized_id = plugin_id.lower()
        return any(item.lower() == normalized_id for item in current)

    @staticmethod
    def __write_membership(
        current: object,
        plugin_id: str,
        target: bool,
    ) -> list[str]:
        """在配置写锁内只增删目标插件，保留其他插件并发变更。"""
        if current is None:
            values: list[str] = []
        elif isinstance(current, list) and all(
            isinstance(item, str) for item in current
        ):
            values = list(current)
        else:
            raise PluginInstallationConflictError(
                "UserInstalledPlugins 当前值不是 JSON 字符串数组"
            )

        normalized_id = plugin_id.lower()
        values = [item for item in values if item.lower() != normalized_id]
        if target:
            values.append(plugin_id)
        return values

    @classmethod
    def __identity_revision(
        cls,
        session: Session,
        plugin_id: str,
    ) -> int | None:
        """读取锁定身份行的 revision；缺行表示 CAS 的 null。"""
        identity = cls.__identity_query(session, plugin_id)
        return identity.revision if identity is not None else None

    @classmethod
    def __write_identity(
        cls,
        session: Session,
        plugin_id: str,
        identity: PluginIdentity | None,
    ) -> None:
        """在调用方事务中写入或删除目标插件身份。"""
        current = cls.__identity_query(session, plugin_id)
        if identity is None:
            if current is not None:
                session.delete(current)
            return
        if identity.plugin_id != plugin_id:
            raise PluginInstallationConflictError(
                "PluginIdentity target 与事务 plugin_id 不一致"
            )
        values = _identity_model_values(identity)
        if current is None:
            session.add(IdentityModel(**values))
        else:
            for key, value in values.items():
                setattr(current, key, value)

    @staticmethod
    def __assert_target_identity(
        record: PluginInstallationRecord,
        identity: PluginIdentity,
    ) -> None:
        """确认目标身份属于当前插件且 revision 只前进一步。"""
        if identity.plugin_id != record.plugin_id:
            raise PluginInstallationConflictError(
                "PluginIdentity target 与事务 plugin_id 不一致"
            )
        expected_revision = (record.identity_before_revision or 0) + 1
        if identity.revision != expected_revision:
            raise PluginInstallationConflictError(
                f"事务 {record.transaction_id} 的 target identity revision "
                f"必须为 {expected_revision}"
            )

    @staticmethod
    def __require_row(session: Session, transaction_id: str) -> PluginInstallation:
        """读取并锁定事务行，缺失时拒绝继续写入。"""
        row = session.execute(
            select(PluginInstallation)
            .where(PluginInstallation.transaction_id == transaction_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise PluginInstallationConflictError(
                f"插件安装事务不存在: {transaction_id}"
            )
        return cast(PluginInstallation, row)

    @classmethod
    def __check_phase(
        cls,
        row: PluginInstallation,
        expected_phase: PluginInstallationPhase | str,
    ) -> PluginInstallationPhase:
        """执行写操作共用的 phase CAS。"""
        expected = cls.__phase(expected_phase)
        try:
            actual = PluginInstallationPhase(row.phase)
        except ValueError as error:
            raise PluginInstallationConflictError(
                f"事务 {row.transaction_id} 的 phase 无效: {row.phase}"
            ) from error
        if actual is not expected:
            raise PluginInstallationConflictError(
                f"事务 {row.transaction_id} phase 已变化: "
                f"expected={expected.value}, actual={actual.value}"
            )
        return actual

    @classmethod
    def __assert_before_state(
        cls,
        record: PluginInstallationRecord,
        session: Session,
        current_membership: object,
    ) -> None:
        """确认目标插件仍处于事务创建时的 before 状态。"""
        membership = cls.__membership_state(current_membership, record.plugin_id)
        revision = cls.__identity_revision(session, record.plugin_id)
        if membership != record.membership_before:
            raise PluginInstallationConflictError(
                f"事务 {record.transaction_id} 的插件 membership 发生漂移"
            )
        if revision != record.identity_before_revision:
            raise PluginInstallationConflictError(
                f"事务 {record.transaction_id} 的插件身份 revision 发生漂移"
            )

    def create(self, record: PluginInstallationRecord) -> PluginInstallationRecord:
        """创建一条事务记录并立即 flush 唯一键竞争。"""
        session = self.__session()
        try:
            with session.begin():
                session.add(
                    PluginInstallation(
                        transaction_id=record.transaction_id,
                        plugin_id=record.plugin_id,
                        phase=record.phase.value,
                        membership_before=record.membership_before,
                        membership_target=record.membership_target,
                        identity_before_revision=record.identity_before_revision,
                        identity_target_revision=record.identity_target_revision,
                        package_existed=record.package_existed,
                        persistent_backup_existed=record.persistent_backup_existed,
                        created_at=record.created_at.isoformat(),
                        updated_at=record.updated_at.isoformat(),
                        schema_version=record.schema_version,
                    )
                )
                session.flush()
                return record
        except IntegrityError as error:
            raise PluginInstallationConflictError(
                f"插件安装事务已存在: {record.transaction_id}"
            ) from error
        finally:
            session.close()

    def get(self, transaction_id: str) -> PluginInstallationRecord | None:
        """按事务 ID 读取记录。"""
        session = self.__session()
        try:
            row = session.execute(
                select(PluginInstallation).where(
                    PluginInstallation.transaction_id == transaction_id
                )
            ).scalar_one_or_none()
            return self.__to_record(row) if row else None
        finally:
            session.close()

    def list(
        self,
        *,
        plugin_id: str | None = None,
    ) -> list[PluginInstallationRecord]:
        """按创建时间稳定列出事务记录。"""
        session = self.__session()
        try:
            statement = select(PluginInstallation).order_by(
                PluginInstallation.created_at,
                PluginInstallation.transaction_id,
            )
            if plugin_id is not None:
                statement = statement.where(PluginInstallation.plugin_id == plugin_id)
            return [
                self.__to_record(row)
                for row in session.execute(statement).scalars()
            ]
        finally:
            session.close()

    def set_target(
        self,
        transaction_id: str,
        *,
        membership_target: bool,
        identity_target: PluginIdentity | None,
        expected_phase: PluginInstallationPhase,
    ) -> PluginInstallationRecord:
        """按 phase CAS 登记目标 membership 和身份 revision，不写业务状态。"""
        if not isinstance(membership_target, bool):
            raise PluginInstallationConflictError("membership_target 必须是布尔值")
        session = self.__session()
        try:
            with session.begin():
                row = self.__require_row(session, transaction_id)
                self.__check_phase(row, expected_phase)
                record = self.__to_record(row)
                if identity_target is not None:
                    self.__assert_target_identity(record, identity_target)
                row.membership_target = membership_target
                row.identity_target_revision = (
                    identity_target.revision if identity_target is not None else None
                )
                row.updated_at = self.__now()
                session.flush()
                return self.__to_record(row)
        finally:
            session.close()

    def commit_target(
        self,
        transaction_id: str,
        *,
        identity_target: PluginIdentity | None,
        expected_phase: PluginInstallationPhase,
    ) -> PluginInstallationRecord:
        """原子提交目标 membership、身份 CAS 和 COMMITTED phase。"""
        def commit(
            session: Session,
            current_membership: object,
        ) -> tuple[PluginInstallationRecord, list[str]]:
            """在配置行锁持有期间完成事务行、身份和 membership 写入。"""
            row = self.__require_row(session, transaction_id)
            self.__check_phase(row, expected_phase)
            record = self.__to_record(row)
            if record.membership_target is None:
                raise PluginInstallationConflictError(
                    f"事务 {transaction_id} 尚未设置 membership target"
                )
            if identity_target is not None:
                self.__assert_target_identity(record, identity_target)
                if identity_target.revision != record.identity_target_revision:
                    raise PluginInstallationConflictError(
                        f"事务 {transaction_id} 的 target identity revision 不匹配"
                    )
            elif record.identity_target_revision is not None:
                raise PluginInstallationConflictError(
                    f"事务 {transaction_id} 缺少 target identity"
                )

            self.__assert_before_state(record, session, current_membership)
            updated_membership = self.__write_membership(
                current_membership,
                record.plugin_id,
                record.membership_target,
            )
            self.__write_identity(session, record.plugin_id, identity_target)
            row.phase = PluginInstallationPhase.COMMITTED.value
            row.updated_at = self.__now()
            session.flush()
            return self.__to_record(row), updated_membership

        try:
            return cast(
                PluginInstallationRecord,
                self.__update_membership_atomically(
                    _INSTALLED_PLUGINS_KEY,
                    commit,
                ),
            )
        except IntegrityError as error:
            raise PluginInstallationConflictError(
                f"插件 {transaction_id} 的身份提交发生唯一键竞争"
            ) from error

    def delete(
        self,
        transaction_id: str,
        *,
        expected_phase: PluginInstallationPhase,
    ) -> bool:
        """按 phase CAS 删除事务记录；缺失记录按幂等删除处理。"""
        session = self.__session()
        try:
            with session.begin():
                row = session.execute(
                    select(PluginInstallation)
                    .where(PluginInstallation.transaction_id == transaction_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if row is None:
                    return False
                self.__check_phase(row, expected_phase)
                session.delete(row)
                session.flush()
                return True
        finally:
            session.close()
