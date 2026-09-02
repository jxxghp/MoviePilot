"""认证、用户与 PassKey 服务的启动组合。"""

from dataclasses import dataclass
from typing import cast

from app.adapters.web.security.access import (
    reset_superuser_token_payload_provider,
    reset_token_identity_validator,
    set_superuser_token_payload_provider,
    set_token_identity_validator,
)
from app.application.configuration import get_configured_system_config
from app.application.security.auth import (
    AuthService,
    AuthUserRepository,
    build_superuser_token_payload,
    configure_auth_service,
    reset_auth_service,
    validate_token_identity,
)
from app.application.security.passkey import (
    PASSKEY_CHALLENGE_TTL_SECONDS,
    PasskeyService,
    configure_passkey_challenge_cache,
    configure_passkey_service,
    reset_passkey_challenge_cache,
    reset_passkey_service,
)
from app.application.security.user import configure_user_lookups, reset_user_lookups
from app.db.adapters.user import SqlAlchemyUserRepository
from app.db.oper.passkey import PassKeyOper
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.cache import TTLCache
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting, update_runtime_setting
from app.startup.composition.context import (
    RepositoryFactory,
    StandaloneRepositoryFactory,
)
from app.startup.composition.database import build_transactional_user_repository


@dataclass(frozen=True, slots=True)
class SecurityComposition:
    """保存认证领域运行时投影所需的持久化工厂。"""

    user_repository: RepositoryFactory
    passkey_repository: RepositoryFactory
    standalone_user: StandaloneRepositoryFactory
    system_config: StandaloneRepositoryFactory
    passkey: StandaloneRepositoryFactory


def _backfill_superuser_setting(users: AuthUserRepository) -> None:
    """用现有数据库管理员补全 V2 升级后缺失的 SUPERUSER。"""
    if str(get_runtime_setting("SUPERUSER") or "").strip():
        return
    user = users.get_active_superuser()
    if user is None:
        return
    success, message = update_runtime_setting("SUPERUSER", user.name)
    if success is False:
        logger.warning(
            f"检测到数据库超级管理员 {user.name}，但自动补全 SUPERUSER 失败："
            f"{message or '未知错误'}"
        )
        return
    logger.info(f"已根据数据库超级管理员自动补全 SUPERUSER：{user.name}")


def configure_security_services() -> SecurityComposition:
    """构造并登记认证、用户查询和 PassKey 服务。"""
    users = build_transactional_user_repository()
    _backfill_superuser_setting(users)
    configure_user_lookups(
        by_id=lambda user_id: build_transactional_user_repository().get_by_id(user_id),
        by_name=lambda username: build_transactional_user_repository().get_by_name(username),
        by_channel=lambda **bindings: build_transactional_user_repository().find_name_by_bindings(bindings),
    )
    configure_auth_service(
        AuthService(
            users=users,
            config=get_configured_system_config(),
            passkeys=PassKeyOper(),
        )
    )
    configure_passkey_challenge_cache(
        TTLCache(
            region="passkey_challenge",
            maxsize=4096,
            ttl=PASSKEY_CHALLENGE_TTL_SECONDS,
        )
    )
    configure_passkey_service(PasskeyService(repository=PassKeyOper()))
    return SecurityComposition(
        user_repository=SqlAlchemyUserRepository,
        passkey_repository=cast(RepositoryFactory, PassKeyOper),
        standalone_user=build_transactional_user_repository,
        system_config=SystemConfigOper,
        passkey=PassKeyOper,
    )


def configure_security_access() -> None:
    """登记 Web 认证边界所需的令牌载荷和当前身份校验端口。"""
    set_superuser_token_payload_provider(build_superuser_token_payload)
    set_token_identity_validator(validate_token_identity)


def reset_security_services() -> None:
    """按发布逆序撤销认证、用户查询和 PassKey 服务。"""
    reset_passkey_service()
    reset_passkey_challenge_cache()
    reset_auth_service()
    reset_user_lookups()


def reset_security_access() -> None:
    """撤销 Web 认证边界的令牌载荷和当前身份校验端口。"""
    reset_superuser_token_payload_provider()
    reset_token_identity_validator()
