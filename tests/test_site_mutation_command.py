from unittest.mock import AsyncMock, Mock

import pytest

from app.application.site.mutation import SiteMutationCommand


def _command(**overrides):
    """构造可观察站点写用例及其依赖。"""
    repository = Mock()
    repository.get_by_id = AsyncMock(return_value=object())
    repository.get_by_domain = AsyncMock(return_value=None)
    repository.stage_create = AsyncMock()
    repository.stage_update = AsyncMock(return_value=True)
    repository.stage_delete = AsyncMock()
    repository.stage_priorities = AsyncMock()
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    dependencies = {
        "repository": repository,
        "unit_of_work": unit_of_work,
        "auth_level_provider": Mock(return_value=2),
        "indexer_loader": AsyncMock(return_value={"name": "Demo", "public": True}),
        "domain_extractor": lambda value: "demo.example",
        "url_normalizer": lambda value: "https://demo.example/",
        "publish_updated": AsyncMock(),
        "publish_deleted": AsyncMock(),
    }
    dependencies.update(overrides)
    return SiteMutationCommand(**dependencies), dependencies


@pytest.mark.asyncio
async def test_create_site_commits_before_updated_event():
    """新增站点必须先提交，再发布站点更新事件。"""
    calls = []
    command, dependencies = _command(
        unit_of_work=Mock(
            commit=AsyncMock(side_effect=lambda: calls.append("commit")),
            rollback=AsyncMock(),
        ),
        publish_updated=AsyncMock(side_effect=lambda _payload: calls.append("event")),
    )

    result = await command.create({"url": "https://demo.example/path"})

    assert result.success is True
    assert calls == ["commit", "event"]
    payload = dependencies["repository"].stage_create.await_args.args[0]
    assert payload["domain"] == "demo.example"
    assert payload["url"] == "https://demo.example/"
    assert payload["name"] == "Demo"
    assert payload["public"] == 1


@pytest.mark.asyncio
async def test_update_site_returns_legacy_not_found_without_writes():
    """更新不存在站点时保持失败响应且不产生事务或事件。"""
    repository = Mock()
    repository.get_by_id = AsyncMock(return_value=None)
    command, dependencies = _command(repository=repository)

    result = await command.update({"id": 7, "url": "https://demo.example"})

    assert result.success is False
    assert result.message == "站点不存在"
    dependencies["unit_of_work"].commit.assert_not_awaited()
    dependencies["publish_updated"].assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_site_commit_failure_rolls_back_without_event():
    """删除提交失败时必须回滚且不得发送 SiteDeleted。"""
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock(side_effect=RuntimeError("commit failed"))
    unit_of_work.rollback = AsyncMock()
    command, dependencies = _command(unit_of_work=unit_of_work)

    with pytest.raises(RuntimeError, match="commit failed"):
        await command.delete(7)

    unit_of_work.rollback.assert_awaited_once_with()
    dependencies["publish_deleted"].assert_not_awaited()


@pytest.mark.asyncio
async def test_update_priorities_uses_one_transaction():
    """批量站点优先级必须由一个请求级事务统一提交。"""
    command, dependencies = _command()
    priorities = [{"id": 1, "pri": 2}, {"id": 2, "pri": 1}]

    result = await command.update_priorities(priorities)

    assert result.success is True
    dependencies["repository"].stage_priorities.assert_awaited_once_with(priorities)
    dependencies["unit_of_work"].commit.assert_awaited_once_with()
