"""
社区勋章主动预热、后台更新和页面非阻塞快照读取回归覆盖。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.application.community import medals
from app.application.community.medals import CommunityMedalService
from app.domain.github import GithubAuthTransportError, GithubMedalSnapshot, GithubUser
from app.runtime.tasks import TaskRegistry
from app.startup import lifecycle


@pytest_asyncio.fixture
async def background(monkeypatch):
    """
    使用受控时钟和传输替身启动真实维护任务，用例结束时统一取消所有任务。
    """
    monkeypatch.setattr(medals, "_REFRESH_COOLDOWN_SECONDS", 0.01)
    settings = {"token": "first", "enabled": True, "time": 1000.0}
    transport = SimpleNamespace(
        get_user=AsyncMock(return_value=GithubUser("artist", 42)),
        get_medal_snapshot=AsyncMock(return_value=GithubMedalSnapshot("t", ({"key": "pioneer"},))),
    )
    enabled = AsyncMock(return_value=True)
    token = AsyncMock(side_effect=lambda: settings["token"])
    service = CommunityMedalService(
        token_reader=token, token_snapshot=lambda: settings["token"],
        local_enabled=lambda: settings["enabled"], enabled_reader=enabled,
        transport=transport, clock=lambda: settings["time"],
    )
    tasks = TaskRegistry()
    yield service, transport, settings, tasks, enabled, token
    assert await tasks.shutdown(timeout_seconds=1)


async def _until(predicate):
    """
    有界等待后台可观察结果，避免依赖测试机器执行速度。

    :param predicate: 表示目标状态已经出现的无副作用断言条件
    """
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_startup_prewarms_without_any_user_request(background):
    """
    启动任务即主动拉取名单，重复启动只有一个 owner，页面查询不再调用外部能力。
    """
    service, transport, _, tasks, enabled, token = background
    service.start(tasks)
    service.start(tasks)
    await _until(lambda: transport.get_medal_snapshot.await_count == 1)
    assert len(tasks.records) == 1
    assert tasks.records[0].owner == "community.medals"
    calls = (enabled.await_count, token.await_count)
    profile = await service.profile()
    assert profile.state == "ready"
    assert profile.user_id == 42
    assert (enabled.await_count, token.await_count) == calls
    assert transport.get_medal_snapshot.await_count == 1


@pytest.mark.asyncio
async def test_periodic_expiry_and_failure_recovery_need_no_page_request(background):
    """
    六小时到期后无需用户请求就主动更新，暂时失败保留旧结果且下一周期自动恢复。
    """
    service, transport, settings, tasks, _, _ = background
    service.start(tasks)
    await _until(lambda: transport.get_medal_snapshot.await_count == 1)
    transport.get_medal_snapshot.side_effect = GithubAuthTransportError("offline")
    settings["time"] += 21600
    await _until(lambda: transport.get_medal_snapshot.await_count == 2)
    assert (await service.profile()).stale
    assert (await service.profile()).medals == ({"key": "pioneer"},)
    transport.get_medal_snapshot.side_effect = None
    transport.get_medal_snapshot.return_value = GithubMedalSnapshot("t2", ({"key": "helper"},))
    settings["time"] += 60
    await _until(lambda: transport.get_medal_snapshot.await_count == 3)
    profile = await service.profile()
    assert not profile.stale
    assert profile.medals == ({"key": "helper"},)


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [True, False])
async def test_blocked_download_never_blocks_snapshot_query(background, cold):
    """
    无缓存时立刻报告预热，有旧缓存时立刻返回旧勋章；二者均不等待外部阻塞或刷新锁。
    """
    service, transport, settings, tasks, _, _ = background
    if not cold:
        await service.update()
        settings["time"] += 21600
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(_user_id):
        """
        在名单读取边界制造可控阻塞。

        :param _user_id: 当前经核验的 GitHub 用户标识
        :return: 释放屏障后的公开名单
        """
        started.set()
        await release.wait()
        return GithubMedalSnapshot("t2", ({"key": "helper"},))

    transport.get_medal_snapshot.side_effect = blocked
    service.start(tasks)
    await asyncio.wait_for(started.wait(), timeout=1)
    result = await asyncio.wait_for(service.profile(), timeout=0.05)
    assert result.refreshing
    assert result.state == ("unavailable" if cold else "ready")
    assert result.medals == (() if cold else ({"key": "pioneer"},))
    release.set()
    await _until(lambda: not service.refreshing)
    assert (await service.profile()).medals == ({"key": "helper"},)


@pytest.mark.asyncio
async def test_remote_feature_check_does_not_delay_cached_query(background):
    """
    开关重新核验阻塞时，读取已有快照也不调用或等待远端开关。
    """
    service, _, _, tasks, enabled, token = background
    await service.update()
    started = asyncio.Event()

    async def blocked():
        """
        暂停远端开关查询，等待测试收尾取消。
        """
        started.set()
        await asyncio.Event().wait()

    enabled.side_effect = blocked
    service.start(tasks)
    await asyncio.wait_for(started.wait(), timeout=1)
    calls = token.await_count
    assert (await asyncio.wait_for(service.profile(), timeout=0.05)).state == "ready"
    assert token.await_count == calls


@pytest.mark.asyncio
async def test_identity_change_discards_inflight_result_and_revokes_old_snapshot(background):
    """
    旧凭据请求尚未结束时换账号，查询立即撤下旧身份且迟到结果不能回填。
    """
    service, transport, settings, tasks, _, _ = background
    await service.update()
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(_user_id):
        """
        持有旧身份的在途名单响应。

        :param _user_id: 旧身份的数字标识
        :return: 释放后到达的旧名单
        """
        started.set()
        await release.wait()
        return GithubMedalSnapshot("old", ({"key": "pioneer"},))

    transport.get_medal_snapshot.side_effect = blocked
    settings["time"] += 60
    update = tasks.create(service.update(force=True), owner="tests.community.update")
    await asyncio.wait_for(started.wait(), timeout=1)
    settings["token"] = "second"
    assert (await service.profile()).medals == ()
    release.set()
    await update
    assert (await service.profile()).medals == ()
    transport.get_medal_snapshot.side_effect = None
    transport.get_user.return_value = GithubUser("other", 99)
    await service.update()
    assert (await service.profile()).user_id == 99


@pytest.mark.asyncio
async def test_local_disable_and_token_removal_are_immediate(background):
    """
    本机关闭或删除凭据立即撤销入口，不等后台下一个周期。
    """
    service, _, settings, _, _, _ = background
    await service.update()
    settings["enabled"] = False
    assert (await service.profile()).state == "disabled"
    assert not service.enabled
    settings["enabled"] = True
    settings["token"] = ""
    assert (await service.profile()).medals == ()
    assert not service.enabled


@pytest.mark.asyncio
async def test_manual_refresh_returns_old_result_then_updates_in_background(background):
    """
    刷新按钮只唤醒后台，当前请求立即返回；后续只读查询可以观察更新结果。
    """
    service, transport, settings, tasks, _, _ = background
    service.start(tasks)
    await _until(lambda: transport.get_medal_snapshot.await_count == 1)
    settings["time"] += 60
    transport.get_medal_snapshot.return_value = GithubMedalSnapshot("new", ({"key": "helper"},))
    result = await service.profile(refresh=True)
    assert result.refreshing
    assert result.medals == ({"key": "pioneer"},)
    await _until(lambda: transport.get_medal_snapshot.await_count == 2)
    assert (await service.profile()).medals == ({"key": "helper"},)


def test_lifecycle_wires_startup_worker_to_host_registry():
    """
    生命周期从真实 HostRuntime 发布后台维护，并复用宿主登记器负责关闭。
    """
    app = FastAPI()
    runtime = MagicMock(spec=lifecycle.HostRuntime)
    community = MagicMock(spec=CommunityMedalService)
    runtime.system = SimpleNamespace(github_community=community)
    app.state.host_runtime = runtime
    app.state.task_registry = TaskRegistry()
    lifecycle.initialize_community_medals(app)
    community.start.assert_called_once_with(app.state.task_registry)
