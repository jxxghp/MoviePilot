"""用户应用服务的请求级事务边界测试。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.security.user import UserService, UserSnapshot, UserUpdateResult


def _configuration_publisher() -> MagicMock:
    """构造可检查的提交后用户配置发布端口。"""
    publisher = MagicMock()
    publisher.rename = AsyncMock()
    publisher.delete = AsyncMock()
    return publisher


@pytest.mark.asyncio
async def test_user_service_commits_staged_mutation() -> None:
    """正式用户写用例必须在仓储暂存成功后提交请求 UoW。"""
    repository = MagicMock()
    repository.async_create = AsyncMock(return_value={"id": 7})
    unit_of_work = MagicMock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    configuration = _configuration_publisher()
    service = UserService(repository, unit_of_work, configuration)

    assert await service.create({"name": "demo"}) == {"id": 7}
    unit_of_work.commit.assert_awaited_once_with()
    unit_of_work.rollback.assert_not_awaited()
    configuration.rename.assert_not_awaited()
    configuration.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_service_rolls_back_failed_mutation() -> None:
    """用户仓储写入失败时不得提交部分事务。"""
    repository = MagicMock()
    repository.async_delete = AsyncMock(side_effect=RuntimeError("write failed"))
    unit_of_work = MagicMock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    configuration = _configuration_publisher()
    service = UserService(repository, unit_of_work, configuration)

    with pytest.raises(RuntimeError, match="write failed"):
        await service.delete(7)

    unit_of_work.rollback.assert_awaited_once_with()
    unit_of_work.commit.assert_not_awaited()
    configuration.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_service_rolls_back_failed_commit() -> None:
    """提交阶段失败时也必须显式回滚当前用户聚合事务。"""
    repository = MagicMock()
    repository.async_update = AsyncMock(
        return_value=UserUpdateResult(
            user=UserSnapshot.build(
                user_id=7,
                name="demo",
                email=None,
                is_active=True,
                is_superuser=False,
                avatar=None,
                is_otp=False,
                permissions=None,
                settings=None,
            ),
            previous_name="old",
        )
    )
    unit_of_work = MagicMock()
    unit_of_work.commit = AsyncMock(side_effect=RuntimeError("commit failed"))
    unit_of_work.rollback = AsyncMock()
    configuration = _configuration_publisher()
    service = UserService(repository, unit_of_work, configuration)

    with pytest.raises(RuntimeError, match="commit failed"):
        await service.update(7, {"name": "demo"})

    unit_of_work.commit.assert_awaited_once_with()
    unit_of_work.rollback.assert_awaited_once_with()
    configuration.rename.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_service_publishes_rename_only_after_commit() -> None:
    """用户改名必须先提交聚合事务，再同步用户名配置快照。"""
    calls: list[str] = []
    repository = MagicMock()
    repository.async_update = AsyncMock(
        return_value=UserUpdateResult(
            user=UserSnapshot.build(
                user_id=7,
                name="new",
                email=None,
                is_active=True,
                is_superuser=False,
                avatar=None,
                is_otp=False,
                permissions=None,
                settings=None,
            ),
            previous_name="old",
        )
    )
    unit_of_work = MagicMock()
    unit_of_work.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
    unit_of_work.rollback = AsyncMock()
    configuration = _configuration_publisher()
    configuration.rename = AsyncMock(side_effect=lambda *_args: calls.append("publish"))
    service = UserService(repository, unit_of_work, configuration)

    result = await service.update(7, {"name": "new"})

    assert result is not None
    assert result.name == "new"
    assert calls == ["commit", "publish"]
    configuration.rename.assert_awaited_once_with("old", "new")


@pytest.mark.asyncio
async def test_user_service_does_not_rollback_committed_publish_failure() -> None:
    """提交后快照发布失败不得伪装成可回滚的用户事务。"""
    repository = MagicMock()
    repository.async_delete = AsyncMock(return_value="member")
    unit_of_work = MagicMock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    configuration = _configuration_publisher()
    configuration.delete = AsyncMock(side_effect=RuntimeError("publish failed"))
    service = UserService(repository, unit_of_work, configuration)

    with pytest.raises(RuntimeError, match="publish failed"):
        await service.delete(7)

    unit_of_work.commit.assert_awaited_once_with()
    unit_of_work.rollback.assert_not_awaited()
