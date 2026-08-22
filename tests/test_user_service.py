"""用户应用服务的请求级事务边界测试。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.security.user import UserService


@pytest.mark.asyncio
async def test_user_service_commits_staged_mutation() -> None:
    """正式用户写用例必须在仓储暂存成功后提交请求 UoW。"""
    repository = MagicMock()
    repository.async_create = AsyncMock(return_value={"id": 7})
    unit_of_work = MagicMock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    service = UserService(repository, unit_of_work)

    assert await service.create({"name": "demo"}) == {"id": 7}
    unit_of_work.commit.assert_awaited_once_with()
    unit_of_work.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_service_rolls_back_failed_mutation() -> None:
    """用户仓储写入失败时不得提交部分事务。"""
    repository = MagicMock()
    repository.async_delete = AsyncMock(side_effect=RuntimeError("write failed"))
    unit_of_work = MagicMock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    service = UserService(repository, unit_of_work)

    with pytest.raises(RuntimeError, match="write failed"):
        await service.delete(7)

    unit_of_work.rollback.assert_awaited_once_with()
    unit_of_work.commit.assert_not_awaited()
