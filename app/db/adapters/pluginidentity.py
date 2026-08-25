"""插件来源身份 Application Port 的 SQLAlchemy 实现。"""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginIdentityConflictError,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
    WritePluginIdentityCommand,
    normalize_physical_plugin_id,
)
from app.db.models.pluginidentity import PluginIdentity as IdentityModel
from app.db.oper.pluginidentity import PluginIdentityOper
from app.db.uow import SqlAlchemyUnitOfWork


def _parse_datetime(value: str | None) -> datetime | None:
    """把数据库 ISO 时间还原为带时区应用值。"""
    return datetime.fromisoformat(value) if value else None


def _to_record(model: IdentityModel) -> PluginIdentity:
    """把持久化模型映射为已校验的应用身份。"""
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
        bound_at=_parse_datetime(model.bound_at),
        payload_applied_at=_parse_datetime(model.payload_applied_at),
    )


def _to_model(identity: PluginIdentity) -> IdentityModel:
    """把应用身份映射为不拥有事务的持久化模型。"""
    return IdentityModel(
        plugin_id=identity.plugin_id,
        normalized_plugin_id=identity.normalized_plugin_id,
        trusted_source_type=identity.trusted_source_type.value,
        trusted_source_key=identity.trusted_source_key,
        binding_basis=identity.binding_basis.value,
        payload_source_type=identity.payload_source_type.value,
        payload_source_key=identity.payload_source_key,
        declared_version=identity.declared_version,
        package_generation=identity.package_generation,
        system_version=identity.system_version,
        supports_v3=identity.supports_v3,
        supports_v3t=identity.supports_v3t,
        payload_receipt=identity.payload_receipt,
        revision=identity.revision,
        created_at=identity.created_at.isoformat(),
        updated_at=identity.updated_at.isoformat(),
        bound_at=identity.bound_at.isoformat() if identity.bound_at else None,
        payload_applied_at=(
            identity.payload_applied_at.isoformat()
            if identity.payload_applied_at
            else None
        ),
    )


class _SqlAlchemyIdentityRepository:
    """绑定一个调用方 Session 的来源身份仓储。"""

    def __init__(self, session: Session) -> None:
        """保存由事务适配器拥有的 Session。"""
        self._oper = PluginIdentityOper(session)

    def get(self, plugin_id: str) -> PluginIdentity | None:
        """读取并映射指定来源身份。"""
        model = self._oper.get_by_plugin_id(plugin_id)
        return _to_record(model) if model else None

    def stage_create(self, identity: PluginIdentity) -> None:
        """暂存首次身份。"""
        try:
            self._oper.stage_create(_to_model(identity))
        except IntegrityError as error:
            raise PluginIdentityConflictError(
                f"插件 {identity.plugin_id} 的来源身份已存在"
            ) from error

    def stage_replace(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> bool:
        """按 revision 条件暂存替换。"""
        return self._oper.stage_replace(
            _to_model(identity),
            expected_revision=expected_revision,
        )


class TransactionalPluginIdentityStore:
    """为每次来源身份读写创建独占同步数据库会话。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由组合根提供的同步 Session 工厂。"""
        self._session_factory = session_factory

    def get(self, plugin_id: str) -> PluginIdentity | None:
        """在短会话内读取指定物理插件身份。"""
        session = self._session_factory()
        try:
            return _SqlAlchemyIdentityRepository(session).get(
                normalize_physical_plugin_id(plugin_id)
            )
        finally:
            session.close()

    def compare_and_set(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int | None,
    ) -> PluginIdentity:
        """在一个事务内执行首次创建或 revision 条件替换。"""
        session = self._session_factory()
        try:
            return WritePluginIdentityCommand(
                repository=_SqlAlchemyIdentityRepository(session),
                unit_of_work=SqlAlchemyUnitOfWork(session),
            ).execute(identity, expected_revision=expected_revision)
        finally:
            session.close()
