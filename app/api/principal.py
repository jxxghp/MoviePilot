"""
API 鉴权依赖向端点暴露的最小当前用户契约。
"""

from typing import Protocol


class ApiPrincipal(Protocol):
    """
    端点身份判断所需的最小用户信息，避免依赖 ORM 实现。

    :ivar id: 已认证用户的数据库主键
    :ivar name: 已认证用户的账号名称
    :ivar is_superuser: 当前用户是否拥有系统管理员权限
    """

    id: int
    name: str
    is_superuser: bool


__all__ = ["ApiPrincipal"]
