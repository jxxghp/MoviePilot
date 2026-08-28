"""用户端点的稳定业务错误映射测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.endpoints.user import create_user, delete_user_by_id, update_user
from app.application.security.user import (
    LastActiveSuperuserError,
    UserNameConflictError,
)


@pytest.mark.asyncio
async def test_create_user_maps_database_name_conflict() -> None:
    """创建竞态触发唯一约束时必须返回既有业务响应。"""
    service = SimpleNamespace(
        create=AsyncMock(side_effect=UserNameConflictError("duplicate"))
    )
    user_input = SimpleNamespace(model_dump=lambda: {
        "name": "duplicate",
        "password": None,
    })

    response = await create_user(
        service=service,
        user_in=user_input,
        current_user=SimpleNamespace(),
    )

    assert response.success is False
    assert response.message == "用户已存在"


@pytest.mark.asyncio
async def test_update_user_maps_name_and_last_admin_conflicts() -> None:
    """改名冲突和最后管理员保护必须保持稳定可读响应。"""
    user_input = SimpleNamespace(model_dump=lambda: {
        "id": 7,
        "name": "renamed",
        "password": None,
    })
    service = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(id=7)),
        update=AsyncMock(side_effect=UserNameConflictError("renamed")),
    )

    response = await update_user(
        service=service,
        user_in=user_input,
        current_user=SimpleNamespace(),
    )

    assert response.success is False
    assert response.message == "用户名已被使用"

    service.update.side_effect = LastActiveSuperuserError("admin")
    response = await update_user(
        service=service,
        user_in=user_input,
        current_user=SimpleNamespace(),
    )
    assert response.success is False
    assert response.message == "必须保留至少一个启用的超级管理员"


@pytest.mark.asyncio
async def test_delete_user_maps_last_admin_conflict() -> None:
    """删除最后管理员时必须返回业务失败且不伪报成功。"""
    service = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(id=7)),
        delete=AsyncMock(side_effect=LastActiveSuperuserError("admin")),
    )

    response = await delete_user_by_id(
        service=service,
        user_id=7,
        current_user=SimpleNamespace(),
    )

    assert response.success is False
    assert response.message == "必须保留至少一个启用的超级管理员"
