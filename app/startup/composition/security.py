"""认证、用户与 PassKey 服务的启动组合。"""

from dataclasses import dataclass
from typing import cast

from app.adapters.web.security.access import (
    reset_superuser_token_payload_provider,
    set_superuser_token_payload_provider,
)
from app.application.configuration import get_configured_system_config
from app.application.security.auth import (
    AuthService,
    build_superuser_token_payload,
    configure_auth_service,
    reset_auth_service,
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


def configure_security_services() -> SecurityComposition:
    """构造并登记认证、用户查询和 PassKey 服务。"""
    configure_user_lookups(
        by_id=lambda user_id: build_transactional_user_repository().get_by_id(user_id),
        by_name=lambda username: build_transactional_user_repository().get_by_name(username),
        by_channel=lambda **bindings: build_transactional_user_repository().find_name_by_bindings(bindings),
    )
    configure_auth_service(
        AuthService(
            users=build_transactional_user_repository(),
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
    """登记 Web 认证边界所需的超级用户令牌载荷提供器。"""
    set_superuser_token_payload_provider(build_superuser_token_payload)


def reset_security_services() -> None:
    """按发布逆序撤销认证、用户查询和 PassKey 服务。"""
    reset_passkey_service()
    reset_passkey_challenge_cache()
    reset_auth_service()
    reset_user_lookups()


def reset_security_access() -> None:
    """撤销 Web 认证边界的超级用户令牌载荷提供器及其缓存。"""
    reset_superuser_token_payload_provider()
