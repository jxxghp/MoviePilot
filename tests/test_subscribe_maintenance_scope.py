"""订阅维护按媒体类型收窄处理范围的回归测试。"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api.endpoints.submaintenance import (
    check_subscribes,
    refresh_subscribes,
    search_subscribes,
)
from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.execution import SubscriptionExecutionAdmission
from app.application.subscription.search import SubscriptionSearchSubmission
from app.chain.subscribe.facade import SubscribeChain
from app.chain.subscribe.query import SubscribeQueryOwner
from app.chain.subscribe.refresh import SubscribeRefreshOwner
from app.db.adapters.subscription import SessionSubscriptionRepository
from app.schemas.types import MediaType


def _snapshot(subscribe_id: int, mtype: MediaType) -> SubscriptionSnapshot:
    """构造指定媒体类型的活动订阅快照。"""
    return SubscriptionSnapshot(
        id=subscribe_id,
        name=f"范围测试 {subscribe_id}",
        type=mtype.value,
        state="R",
        media_id=str(subscribe_id),
    )


@pytest.mark.parametrize(
    ("endpoint", "job_id"),
    [(refresh_subscribes, "subscribe_refresh"), (check_subscribes, "subscribe_tmdb")],
)
def test_maintenance_endpoint_forwards_media_type(endpoint, job_id):
    """管理员维护端点把当前媒体类型传给对应调度任务。"""
    with patch("app.api.endpoints.submaintenance.get_scheduler") as scheduler:
        response = endpoint(
            subscription_type=MediaType.TV,
            current_user=SimpleNamespace(name="admin", is_superuser=True),
        )

    assert response.success
    scheduler.return_value.start.assert_called_once_with(job_id, mtype=MediaType.TV.value)


def test_search_endpoint_forwards_media_type_to_command():
    """管理员搜索端点把当前媒体类型传给搜索用例。"""
    command = Mock()
    command.execute = AsyncMock(
        return_value=SubscriptionSearchSubmission(
            batch_ids=("batch-1",),
            target_count=1,
            queued_count=1,
            ongoing_count=0,
            single=False,
        )
    )

    response = asyncio.run(
        search_subscribes(
            subscription_type=MediaType.MUSIC,
            command=command,
            current_user=SimpleNamespace(name="admin", is_superuser=True),
        )
    )

    assert response.success
    assert command.execute.await_args.kwargs["mtype"] == MediaType.MUSIC.value


def test_query_owner_only_returns_sites_for_requested_media_type():
    """刷新站点范围只采集当前媒体类型，并在无匹配订阅时停止刷新。"""
    movie = _snapshot(1, MediaType.MOVIE)
    tv = _snapshot(2, MediaType.TV)
    owner = object.__new__(SubscribeQueryOwner)
    owner.subscription_repository = SimpleNamespace(list=lambda: [movie, tv])
    owner.get_states_for_search = lambda _state: "R,P"
    owner.get_sub_sites = lambda subscribe: [subscribe.id]

    assert owner.get_subscribed_sites(MediaType.TV.value) == [tv.id]
    assert owner.get_subscribed_sites(MediaType.MUSIC.value) is None


def test_refresh_check_only_processes_requested_media_type():
    """订阅元数据检查只把当前媒体类型交给单订阅检查器。"""
    movie = _snapshot(1, MediaType.MOVIE)
    tv = _snapshot(2, MediaType.TV)
    owner = object.__new__(SubscribeRefreshOwner)
    owner.subscription_repository = SimpleNamespace(list=lambda: [movie, tv])
    processed = []

    def check_subscription(subscribe, _fresh_fact_lease, **_kwargs):
        """记录一次被范围过滤后的元数据检查。"""
        processed.append(subscribe.id)
        return subscribe

    owner._check_subscription = check_subscription
    owner.check(mtype=MediaType.TV.value)

    assert processed == [tv.id]


def test_match_only_processes_requested_media_type():
    """订阅资源匹配只遍历当前媒体类型的活动订阅。"""
    movie = _snapshot(1, MediaType.MOVIE)
    tv = _snapshot(2, MediaType.TV)
    owner = object.__new__(SubscribeChain)
    owner.subscription_repository = SimpleNamespace(
        list=lambda _state: [movie, tv],
        get=lambda subscribe_id: {movie.id: movie, tv.id: tv}.get(subscribe_id),
    )
    owner.get_states_for_search = lambda _state: "R,P"
    owner._match_lock = threading.Lock()
    owner._subscription_execution_admission = SubscriptionExecutionAdmission()
    owner._SUBSCRIPTION_EXECUTION_TTL = 3600 * 2
    owner._prepare_match_torrents = lambda torrents: torrents
    processed = []

    def match_subscription(**kwargs):
        """记录一次被范围过滤后的资源匹配。"""
        processed.append(kwargs["subscribe"].id)
        return "skipped"

    owner._match_subscription = match_subscription
    owner.match({"example.org": []}, mtype=MediaType.TV.value)

    assert processed == [tv.id]


@pytest.mark.asyncio
async def test_session_repository_filters_admin_search_ids_by_media_type():
    """请求级订阅适配器为管理员保留全局权限但按类型裁剪编号。"""
    movie = _snapshot(1, MediaType.MOVIE)
    tv = _snapshot(2, MediaType.TV)
    repository = object.__new__(SessionSubscriptionRepository)
    repository.async_list = AsyncMock(return_value=[movie, tv])
    repository.async_list_by_username = AsyncMock()

    ids = await repository.list_search_ids(None, "R", mtype=MediaType.TV.value)

    assert ids == [tv.id]
    repository.async_list.assert_awaited_once_with("R")
    repository.async_list_by_username.assert_not_awaited()
