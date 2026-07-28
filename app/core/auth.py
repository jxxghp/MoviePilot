import secrets
import threading
import time
from datetime import timedelta
from typing import Any, Optional

from app import schemas
from app.core import security
from app.core.config import settings
from app.db.models.user import User
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.sites import SitesHelper
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


class AuthTicketStore(metaclass=Singleton):
    """
    插件认证一次性票据存储。
    """

    _ttl_seconds = 120
    _max_items = 1024

    def __init__(self):
        """
        初始化内存票据缓存。
        """
        self._tickets: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create(self, user_id: int, provider_id: str, metadata: Optional[dict[str, Any]] = None) -> str:
        """
        创建短时一次性登录票据。

        :param user_id: 已通过插件认证的本地用户 ID
        :param provider_id: 认证提供方 ID
        :param metadata: 插件侧附加信息
        :return: 一次性票据字符串
        """
        ticket = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._cleanup(now)
            self._tickets[ticket] = {
                "user_id": int(user_id),
                "provider_id": provider_id,
                "metadata": metadata or {},
                "created_at": now,
            }
        return ticket

    def consume(self, ticket: str) -> Optional[dict[str, Any]]:
        """
        消费并删除一次性登录票据。

        :param ticket: 登录票据
        :return: 票据数据，票据不存在或过期时返回 None
        """
        if not ticket:
            return None
        now = time.time()
        with self._lock:
            data = self._tickets.pop(ticket, None)
            self._cleanup(now)
        if not data:
            return None
        if now - float(data.get("created_at") or 0) > self._ttl_seconds:
            return None
        return data

    def _cleanup(self, now: Optional[float] = None) -> None:
        """
        清理过期或过量的票据缓存。

        :param now: 当前时间戳，未传入时自动读取
        """
        current = now or time.time()
        expired = [
            key
            for key, value in self._tickets.items()
            if current - float(value.get("created_at") or 0) > self._ttl_seconds
        ]
        for key in expired:
            self._tickets.pop(key, None)
        if len(self._tickets) <= self._max_items:
            return
        ordered = sorted(
            self._tickets.items(),
            key=lambda item: float(item[1].get("created_at") or 0),
        )
        for key, _ in ordered[: len(self._tickets) - self._max_items]:
            self._tickets.pop(key, None)


def create_plugin_auth_ticket(user_id: int, provider_id: str, metadata: Optional[dict[str, Any]] = None) -> str:
    """
    为插件认证成功的用户创建一次性登录票据。

    :param user_id: 本地用户 ID
    :param provider_id: 认证提供方 ID
    :param metadata: 插件侧附加信息
    :return: 一次性票据字符串
    """
    return AuthTicketStore().create(user_id=user_id, provider_id=provider_id, metadata=metadata)


def consume_plugin_auth_ticket(ticket: str) -> Optional[dict[str, Any]]:
    """
    消费插件认证登录票据。

    :param ticket: 登录票据
    :return: 票据数据，票据不存在或过期时返回 None
    """
    return AuthTicketStore().consume(ticket)


def build_token_response(user: User) -> schemas.Token:
    """
    使用系统统一逻辑构造登录 Token 响应。

    :param user: 已认证的本地用户
    :return: 标准 Token 响应
    """
    level = SitesHelper().auth_level
    show_wizard = (
        not SystemConfigOper().get(SystemConfigKey.SetupWizardState)
        and not settings.ADVANCED_MODE
    )
    return schemas.Token(
        access_token=security.create_access_token(
            userid=user.id,
            username=user.name,
            super_user=user.is_superuser,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
            level=level,
        ),
        token_type="bearer",
        super_user=user.is_superuser,
        user_id=user.id,
        user_name=user.name,
        avatar=user.avatar,
        level=level,
        permissions=user.permissions or {},
        wizard=show_wizard,
    )
