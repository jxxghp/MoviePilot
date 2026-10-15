"""社区贡献认定、Token 隔离与 GitHub 协议失败的回归覆盖。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.external.github import GithubAuthClient
from app.api.endpoints.github import get_community_profile
from app.application.community.contributions import CommunityContributionService
from app.domain.github import (
    COMMUNITY_REPOSITORIES,
    GithubAuthTransportError,
    GithubRepositoryContribution,
    GithubUser,
)


@pytest.fixture
def community():
    """提供不访问真实凭据或网络的可控贡献服务。"""
    settings = {"token": "first", "time": 1000, "counts": {}}

    async def token_reader():
        """返回测试凭据。"""
        return settings["token"]

    async def contribution(_token, _user, repository, area, _branch):
        """按仓库返回注入的两类证据。"""
        commits, prs = settings["counts"].get(repository, (0, 0))
        return GithubRepositoryContribution(repository, area, commits, prs)

    transport = SimpleNamespace(
        get_user=AsyncMock(return_value=GithubUser("artist", 42)),
        get_repository_contribution=AsyncMock(side_effect=contribution),
    )
    service = CommunityContributionService(
        token_reader=token_reader, transport=transport, clock=lambda: settings["time"],
    )
    return service, transport, settings


@pytest.mark.asyncio
@pytest.mark.parametrize("count,tier", [(0, None), (1, "bronze"), (9, "bronze"), (10, "silver"),
                                        (49, "silver"), (50, "gold"), (199, "gold"), (200, "core")])
async def test_levels_and_single_frontend_specialty(community, count, tier):
    """等级覆盖所有阈值边界，同仓 PR 与提交不重复相加。"""
    service, _, settings = community
    settings["counts"] = {"MoviePilot-Frontend": (count, count)}
    result = await service.profile()
    assert result.tier == tier
    assert result.total == count
    assert result.specialty == ("frontend" if count else None)
    assert result.state == "ready"


@pytest.mark.asyncio
async def test_cross_repository_specialty_and_plugin_only(community):
    """插件独立称号与跨领域生态称号分别认定，切换 Token 不复用旧结果。"""
    service, _, settings = community
    settings["counts"] = {"MoviePilot-Plugins": (8, 12)}
    assert (await service.profile()).specialty == "plugins"
    settings.update(token="second", counts={"MoviePilot-Plugins": (8, 12), "MoviePilot": (4, 2)})
    result = await service.profile()
    assert result.specialty == "ecosystem"
    assert result.total == 16


@pytest.mark.asyncio
async def test_cache_refresh_cooldown_and_concurrent_readers(community):
    """同一 Token 的并发读取合并，刷新按钮不能绕过一分钟冷却。"""
    service, transport, settings = community
    first, second = await asyncio.gather(service.profile(), service.profile())
    assert first == second
    assert transport.get_user.await_count == 1
    await service.profile(refresh=True)
    assert transport.get_user.await_count == 1
    settings["time"] += 61
    await service.profile(refresh=True)
    assert transport.get_user.await_count == 2


@pytest.mark.asyncio
async def test_failure_preserves_only_same_token_verified_snapshot(community):
    """限流保留旧快照并标记过期，换 Token 后不能借用上一人的徽章。"""
    service, transport, settings = community
    settings["counts"] = {"MoviePilot": (50, 1)}
    original = await service.profile()
    settings["time"] += 61
    transport.get_repository_contribution.side_effect = GithubAuthTransportError("rate limited")
    stale = await service.profile(refresh=True)
    assert stale.tier == original.tier
    assert stale.checked_at == original.checked_at
    assert stale.stale
    settings["token"] = "second"
    failed = await service.profile()
    assert failed.state == "unavailable"
    assert failed.tier is None
    assert failed.login is None


@pytest.mark.asyncio
async def test_unauthorized_or_removed_token_removes_badge(community):
    """失效和删除凭据都不能继续显示已撤销身份的徽章。"""
    service, transport, settings = community
    settings["counts"] = {"MoviePilot": (5, 0)}
    assert (await service.profile()).tier
    settings["time"] += 61
    transport.get_user.side_effect = GithubAuthTransportError("expired", unauthorized=True)
    assert (await service.profile(refresh=True)).tier is None
    settings["token"] = ""
    assert (await service.profile()).state == "unconfigured"


@pytest.mark.asyncio
async def test_token_switch_during_query_discards_late_result(community):
    """网络请求期间变更凭据，旧身份的迟到结果不能点亮菜单。"""
    service, transport, settings = community

    async def user(_token):
        """模拟用户在身份查询期间清除凭据。"""
        settings["token"] = ""
        return GithubUser("artist", 42)

    transport.get_user.side_effect = user
    settings["counts"] = {"MoviePilot": (500, 1)}
    result = await service.profile()
    assert result.tier is None
    assert result.login is None


@pytest.mark.asyncio
async def test_endpoint_returns_sanitized_profile(community):
    """API 只投影贡献事实，不包含服务器 Token。"""
    service, _, settings = community
    settings["counts"] = {"MoviePilot-Wiki": (1, 1)}
    reply = await get_community_profile(refresh=False, community=service)
    assert reply.success
    assert reply.data.specialty == "docs"
    assert "token" not in reply.model_dump_json().lower()
    assert len(COMMUNITY_REPOSITORIES) == 9


@pytest.mark.asyncio
async def test_adapter_uses_complete_search_and_authored_commit_pagination(monkeypatch):
    """作者提交统计解析末页数字，绝不请求 Link 中的任意地址。"""
    client = GithubAuthClient()
    responses = [
        SimpleNamespace(status_code=200, json=lambda: {"total_count": 2, "incomplete_results": False}),
        SimpleNamespace(status_code=200, json=lambda: [{"sha": "one"}],
                        headers={"Link": '<https://untrusted.invalid/?page=12>; rel="last"'}),
    ]
    request = SimpleNamespace(get_res=AsyncMock(side_effect=responses))
    monkeypatch.setattr(client, "_request", lambda **_: request)
    item = await client.get_repository_contribution("secret", GithubUser("artist", 42), "MoviePilot", "backend", "v3")
    assert item.commits == 12
    assert item.merged_prs == 2
    assert all(call.args[0].startswith("https://api.github.com/") for call in request.get_res.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    SimpleNamespace(status_code=403),
    SimpleNamespace(status_code=200, json=lambda: {"total_count": 1, "incomplete_results": True}),
    SimpleNamespace(status_code=200, json=lambda: {"total_count": -1, "incomplete_results": False}),
])
async def test_adapter_does_not_turn_remote_failure_into_zero(monkeypatch, response):
    """限流、不完整搜索和无效统计必须显式失败。"""
    client = GithubAuthClient()
    monkeypatch.setattr(client, "_request", lambda **_: SimpleNamespace(get_res=AsyncMock(return_value=response)))
    with pytest.raises(GithubAuthTransportError):
        await client.get_repository_contribution("secret", GithubUser("artist", 42), "MoviePilot", "backend", "v3")
