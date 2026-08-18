"""API 鉴权依赖向端点暴露的最小当前用户契约。"""

from typing import Protocol


class ApiPrincipal(Protocol):
    """隔离端点身份判断与 ORM User 实现。"""

    id: int
    name: str
    is_superuser: bool


__all__ = ["ApiPrincipal"]
