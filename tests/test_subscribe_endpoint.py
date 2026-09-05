import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.responses import Response

from app.api.endpoints.subscribe import create_subscribe
from app.application.outbox import ClaimedOutboxMessage
from app.application.subscription.contract import (
    SubscriptionHistorySnapshot,
    SubscriptionPatch,
    SubscriptionSnapshot,
)
from app.application.subscription.mutation import SubscriptionMutationService
from app.application.subscription.query import SubscriptionQueryService
from app.runtime.events import eventmanager
from app.schemas.subscribe import Subscribe
from app.schemas.types import EventType, MediaSource, MediaType


def _subscription_query(
    repository: "_SubscriptionRepositoryFake",
    history_repository: "_SubscriptionHistoryRepositoryFake | None" = None,
) -> SubscriptionQueryService:
    """构造使用 typed 内存仓储的订阅查询服务。"""
    return SubscriptionQueryService(
        repository=repository,
        async_repository=repository,
        history_repository=history_repository,
    )


def _subscription_mutation(
    repository: "_SubscriptionRepositoryFake",
    history_repository: "_SubscriptionHistoryRepositoryFake | None" = None,
) -> SubscriptionMutationService:
    """构造使用 typed 内存仓储的订阅写服务。"""
    outbox = _EndpointOutbox()

    async def publish_modified(payload: dict) -> None:
        """把测试服务提交后的修改事件交给真实事件边界。"""
        await eventmanager.async_send_event(EventType.SubscribeModified, payload)

    return SubscriptionMutationService(
        repository=repository,
        unit_of_work=_EndpointUnitOfWork(),
        outbox=outbox,
        dispatch_store=outbox,
        publish_modified=publish_modified,
        history_repository=history_repository,
    )


class _EndpointUnitOfWork:
    """为内存 endpoint 仓储提供无副作用的事务端口。"""

    async def commit(self) -> None:
        """确认内存更新。"""

    async def rollback(self) -> None:
        """结束失败的内存更新。"""


class _EndpointOutbox:
    """让 endpoint 测试观察服务层的一次即时 outbox 派发。"""

    async def stage(self, _intent, _now) -> None:
        """接受内存 intent。"""

    async def claim_by_event_key(self, event_key, _now, _lease_until):
        """认领当前测试刚暂存的 intent。"""
        return ClaimedOutboxMessage(
            message_id=1,
            event_key=event_key,
            topic="subscribe.modified",
            payload={},
            payload_version=1,
            attempt=1,
        )

    async def complete(self, _message_id, _attempt, _completed_at) -> bool:
        """确认服务只完成一次事件派发。"""
        return True

    async def retry(self, _message_id, _attempt, **_kwargs) -> bool:
        """允许失败路径释放内存 lease。"""
        return True


class TestSubscribeEndpoint:
    """
    订阅接口回归测试。
    """

    def test_read_subscribes_scopes_regular_user_and_keeps_superuser_global(self):
        """
        普通用户只能看到自己创建的订阅，超级用户保留全局视图。
        """
        from app.api.endpoints.subscribe import list_subscribes, read_subscribes

        own = _EndpointSubscribe(id=1, username="alice", name="自己的订阅")
        other = _EndpointSubscribe(id=2, username="bob", name="他人的订阅")
        legacy = _EndpointSubscribe(id=3, username=None, name="旧订阅")
        all_subscribes = [own, other, legacy]
        repository = _SubscriptionRepositoryFake(*all_subscribes)

        api_token_result = asyncio.run(list_subscribes(query=_subscription_query(repository), _="api-token"))
        assert [sub.id for sub in api_token_result] == [1, 2, 3]

        regular_result = asyncio.run(
            read_subscribes(
                query=_subscription_query(repository),
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )
        assert [sub.id for sub in regular_result] == [1]

        superuser_result = asyncio.run(
            read_subscribes(
                query=_subscription_query(repository),
                current_user=_EndpointUser(name="admin", is_superuser=True),
            )
        )
        assert [sub.id for sub in superuser_result] == [1, 2, 3]

    def test_read_subscribe_hides_other_and_legacy_from_regular_user(self):
        """
        订阅详情按 owner 隐藏他人和 legacy 订阅，避免泄露订阅行存在性。
        """
        from app.api.endpoints.subscribe import read_subscribe

        current_user = _EndpointUser(name="alice", is_superuser=False)
        cases = [
            (_EndpointSubscribe(id=1, username="alice", name="自己的订阅"), 1),
            (_EndpointSubscribe(id=2, username="bob", name="他人的订阅"), None),
            (_EndpointSubscribe(id=3, username=None, name="旧订阅"), None),
        ]

        for subscribe, expected_id in cases:
            repository = _SubscriptionRepositoryFake(subscribe)
            result = asyncio.run(
                read_subscribe(
                    subscribe_id=subscribe.id,
                    query=_subscription_query(repository),
                    current_user=current_user,
                )
            )

            assert getattr(result, "id", None) == expected_id

    def test_delete_subscribe_delegates_identity_without_database_access(self):
        """按 ID 删除端点只映射用户身份，并返回已提交状态。"""
        from app.api.endpoints.subscribe import delete_subscribe

        command = SimpleNamespace(execute_with_status=AsyncMock(return_value="deleted"))
        response = asyncio.run(
            delete_subscribe(
                subscribe_id=7,
                command=command,
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )

        assert response.success
        assert response.data.status == "deleted"
        command.execute_with_status.assert_awaited_once()
        subscribe_id, actor = command.execute_with_status.await_args.args
        assert subscribe_id == 7
        assert actor.username == "alice"
        assert not actor.is_superuser

    @pytest.mark.parametrize(
        ("status", "expected_code"),
        [("not_found", 404), ("forbidden", 403)],
    )
    def test_delete_subscribe_exposes_missing_and_forbidden_status(self, status, expected_code):
        """按 ID 删除端点必须让调用方区分目标不存在与无权访问。"""
        from app.api.endpoints.subscribe import delete_subscribe

        command = SimpleNamespace(execute_with_status=AsyncMock(return_value=status))
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                delete_subscribe(
                    subscribe_id=7,
                    command=command,
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        assert error.value.status_code == expected_code

    def test_manage_permission_does_not_allow_cross_user_update(self):
        """
        manage 权限不等于跨用户订阅管理权限，普通用户不能修改他人或 legacy 订阅。
        """
        from app.api.endpoints.subscribe import update_subscribe

        manage_user = _EndpointUser(
            name="alice",
            is_superuser=False,
            permissions={"manage": True},
        )

        for subscribe in [
            _EndpointSubscribe(
                id=2,
                username="bob",
                name="他人的订阅",
                total_episode=8,
                lack_episode=2,
            ),
            _EndpointSubscribe(
                id=3,
                username=None,
                name="旧订阅",
                total_episode=8,
                lack_episode=2,
            ),
        ]:
            repository = _SubscriptionRepositoryFake(subscribe)
            with patch(
                "app.runtime.events.eventmanager.async_send_event",
                new=AsyncMock(),
            ) as send_event:
                response = asyncio.run(
                    update_subscribe(
                        subscribe_in=Subscribe(
                            id=subscribe.id,
                            name="改名",
                            total_episode=8,
                            lack_episode=2,
                        ),
                        mutation=_subscription_mutation(repository),
                        current_user=manage_user,
                    )
                )

            assert not response.success
            assert response.message == "订阅不存在"
            send_event.assert_not_awaited()

    def test_owner_can_update_own_subscribe(self):
        """
        owner 可以继续管理自己创建的订阅。
        """
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=4,
            username="alice",
            name="旧标题",
            total_episode=8,
            lack_episode=2,
            vote=0.0,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        repository = _SubscriptionRepositoryFake(subscribe)

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=Subscribe(
                        id=4,
                        name="新标题",
                        total_episode=8,
                        lack_episode=2,
                    ),
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        assert response.success
        send_event.assert_awaited_once()

    def test_update_subscribe_preserves_existing_owner(self):
        """
        普通更新不得允许请求体改写订阅 owner。
        """
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=12,
            username="alice",
            name="旧标题",
            total_episode=8,
            lack_episode=2,
            vote=0.0,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        subscribe_in = Subscribe(
            id=12,
            username="bob",
            name="新标题",
            total_episode=8,
            lack_episode=2,
        )
        repository = _SubscriptionRepositoryFake(subscribe)

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        assert response.success
        assert subscribe.username == "alice"
        event_type, payload = send_event.await_args.args
        assert event_type == EventType.SubscribeModified
        assert "username" not in payload["fields"]
        assert payload["subscribe_info"]["username"] == "alice"

    def test_update_subscribe_preserves_existing_media_identity_when_omitted(self):
        """普通字段更新未提交身份时，不得把已有媒体身份清空。"""
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=24,
            username="alice",
            name="旧标题",
            media_source=MediaSource.TMDB,
            media_id="12345",
            total_episode=8,
            lack_episode=2,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        repository = _SubscriptionRepositoryFake(subscribe)

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=Subscribe(id=24, name="新标题"),
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        assert response.success
        updated = repository.get(24)
        assert updated is not None
        assert updated.media_source == MediaSource.TMDB
        assert updated.media_id == "12345"

    def test_update_subscribe_clears_existing_media_identity_with_empty_pair(self):
        """更新同时显式提交两个空身份字段时，应清空存量身份。"""
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=26,
            username="alice",
            name="旧标题",
            media_source=MediaSource.TMDB,
            media_id="12345",
            total_episode=8,
            lack_episode=2,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        repository = _SubscriptionRepositoryFake(subscribe)

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=Subscribe(
                        id=26,
                        name="新标题",
                        media_source="",
                        media_id="",
                    ),
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        assert response.success
        updated = repository.get(26)
        assert updated is not None
        assert updated.media_source is None
        assert updated.media_id is None

    def test_update_subscribe_rejects_partial_media_identity(self):
        """更新媒体身份时只提交来源或 ID 之一应在 Schema 边界直接拒绝。"""
        with pytest.raises(ValidationError):
            Subscribe(id=25, media_source=MediaSource.Douban)

    def test_update_subscribe_preserves_recognized_music_entity(self):
        """普通编辑不得把专辑改为单曲或覆盖整专完成判定所需的曲目总数。"""
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=23,
            username="alice",
            name="叶惠美",
            type=MediaType.MUSIC.value,
            music_type="album",
            total_tracks=11,
            total_episode=0,
            lack_episode=0,
            vote=0.0,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        subscribe_in = Subscribe(
            id=23,
            name="叶惠美",
            type=MediaType.MUSIC.value,
            music_type="recording",
            total_tracks=1,
        )
        repository = _SubscriptionRepositoryFake(subscribe)

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        assert response.success
        updated = repository.get(23)
        assert updated is not None
        assert updated.type == MediaType.MUSIC.value
        assert updated.music_type == "album"
        assert updated.total_tracks == 11

    def test_superuser_can_update_other_and_legacy_subscribe(self):
        """
        超级用户可以管理他人和 legacy 订阅。
        """
        from app.api.endpoints.subscribe import update_subscribe_status

        current_user = _EndpointUser(name="admin", is_superuser=True)
        for subscribe in [
            _EndpointSubscribe(id=5, username="bob", state="R", name="他人的订阅"),
            _EndpointSubscribe(id=6, username=None, state="R", name="旧订阅"),
        ]:
            repository = _SubscriptionRepositoryFake(subscribe)
            with patch(
                "app.runtime.events.eventmanager.async_send_event",
                new=AsyncMock(),
            ) as send_event:
                response = asyncio.run(
                    update_subscribe_status(
                        subid=subscribe.id,
                        state="S",
                        mutation=_subscription_mutation(repository),
                        current_user=current_user,
                    )
                )

            assert response.success
            send_event.assert_awaited_once()
            updated = repository.get(subscribe.id)
            assert updated is not None
            assert updated.state == "S"

    def test_share_subscribe_requires_local_owner(self):
        """
        分享本地订阅前必须确认当前用户有权读取该订阅行。
        """
        from app.api.endpoints.subscribe import subscribe_share
        from app.schemas.subscribe import SubscribeShare

        other = _EndpointSubscribe(id=7, username="bob", name="他人的订阅")
        repository = _SubscriptionRepositoryFake(other)

        with patch(
            "app.api.endpoints.subscribe.MoviePilotServerHelper.async_sub_share",
            new=AsyncMock(return_value=(True, "")),
        ) as sub_share:
            response = asyncio.run(
                subscribe_share(
                    sub=SubscribeShare(
                        subscribe_id=7,
                        share_title="分享",
                        share_comment="",
                        share_user="alice",
                    ),
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        assert not response.success
        assert response.message == "订阅不存在"
        sub_share.assert_not_awaited()

    def test_subscribe_media_identity_returns_owner_when_other_candidate_matches_first(self):
        """
        按媒体查询订阅时，他人订阅不能挡住当前用户自己的订阅。
        """
        from app.api.endpoints.subscribe import subscribe_media_identity

        other = _EndpointSubscribe(id=13, username="bob", media_source="themoviedb", media_id="123", season=1)
        own = _EndpointSubscribe(id=14, username="alice", media_source="themoviedb", media_id="123", season=1)
        repository = _SubscriptionRepositoryFake(other, own)

        result = asyncio.run(
            subscribe_media_identity(
                media_id="123",
                media_source=MediaSource.TMDB,
                season=1,
                query=_subscription_query(repository),
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )

        assert result.id == 14

    def test_subscribe_media_identity_distinguishes_recording_and_album_entities(self):
        """同一来源身份下查询专辑时不能返回单曲订阅。"""
        from app.api.endpoints.subscribe import subscribe_media_identity

        recording = _EndpointSubscribe(
            id=21,
            username="alice",
            type=MediaType.MUSIC.value,
            music_type="recording",
            media_source="musicbrainz",
            media_id="shared-id",
        )
        album = _EndpointSubscribe(
            id=22,
            username="alice",
            type=MediaType.MUSIC.value,
            music_type="album",
            media_source="musicbrainz",
            media_id="shared-id",
        )
        repository = _SubscriptionRepositoryFake(recording, album)

        result = asyncio.run(
            subscribe_media_identity(
                media_id="shared-id",
                media_source=MediaSource.MusicBrainz,
                music_type="album",
                query=_subscription_query(repository),
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )

        assert result.id == 22
        assert repository.async_list_by_media_identity.await_args.kwargs["music_type"] == "album"

    def test_subscribe_media_identity_does_not_fallback_to_title(self):
        """统一身份未命中时不得按标题串联其他来源的订阅。"""
        from app.api.endpoints.subscribe import subscribe_media_identity

        repository = _SubscriptionRepositoryFake()
        result = asyncio.run(
            subscribe_media_identity(
                media_id="legacy-recording",
                media_source=MediaSource.MusicBrainz,
                title="周杰伦 - 晴天",
                music_type="recording",
                query=_subscription_query(repository),
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )

        assert result.id is None
        repository.async_list_by_title.assert_not_awaited()

    def test_delete_subscribe_by_media_identity_deletes_owner_candidate(self):
        """
        按媒体删除端点应把媒体身份和当前用户交给应用命令。
        """
        from app.api.endpoints.subscribe import delete_subscribe_by_media_identity

        command = SimpleNamespace(execute=AsyncMock(return_value=1))
        response = asyncio.run(
            delete_subscribe_by_media_identity(
                media_id="douban-1",
                media_source=MediaSource.Douban,
                command=command,
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )

        assert response.success
        command.execute.assert_awaited_once()
        media_source, media_id, season, music_type, actor = command.execute.await_args.args
        assert media_source == MediaSource.Douban
        assert media_id == "douban-1"
        assert season is None
        assert music_type is None
        assert actor.username == "alice"
        assert not actor.is_superuser

    def test_delete_subscribe_by_media_identity_forwards_music_entity(self):
        """取消专辑订阅时必须把实体类型传给统一身份查询。"""
        from app.api.endpoints.subscribe import delete_subscribe_by_media_identity

        command = SimpleNamespace(execute=AsyncMock(return_value=0))
        response = asyncio.run(
            delete_subscribe_by_media_identity(
                media_id="release-group-1",
                media_source=MediaSource.MusicBrainz,
                music_type="album",
                command=command,
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )

        assert response.success
        command.execute.assert_awaited_once()
        assert command.execute.await_args.args[:4] == (
            MediaSource.MusicBrainz,
            "release-group-1",
            None,
            "album",
        )

    def test_search_subscribes_regular_user_schedules_only_owned_rows(self):
        """
        普通用户批量搜索把用户身份交给应用命令。
        """
        from app.api.endpoints.subscribe import search_subscribes
        from app.application.subscription.search import SubscriptionSearchSubmission

        command = SimpleNamespace(
            execute=AsyncMock(
                return_value=SubscriptionSearchSubmission(
                    batch_ids=("batch-1",),
                    target_count=2,
                    queued_count=1,
                    ongoing_count=1,
                    single=False,
                )
            )
        )
        response = asyncio.run(
            search_subscribes(
                command=command,
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )

        assert response.success
        assert response.message == "已安排 1 个订阅搜索，另有 1 个正在处理中"
        assert response.data.queued_count == 1
        command.execute.assert_awaited_once()
        actor = command.execute.await_args.args[0]
        assert actor.username == "alice"
        assert not actor.is_superuser

    def test_subscribe_files_hides_other_user_row(self):
        """
        订阅文件接口不能向普通用户暴露他人的订阅文件信息。
        """
        from app.api.endpoints.subscribe import subscribe_files

        other = _EndpointSubscribe(id=19, username="bob", name="他人的订阅")
        repository = _SubscriptionRepositoryFake(other)

        with patch("app.api.endpoints.subscribe.SubscribeChain") as subscribe_chain:
            result = subscribe_files(
                subscribe_id=19,
                repository=repository,
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )

        assert result.episodes == {}
        subscribe_chain.return_value.subscribe_files_info.assert_not_called()

    def test_subscribe_files_uses_sync_query_repository(self):
        """文件统计使用绑定同步 Session 的查询仓储读取稳定快照。"""
        from app.api.endpoints.subscribe import subscribe_files

        own = _EndpointSubscribe(id=21, username="alice", name="自己的订阅")
        repository = _SubscriptionRepositoryFake(own)
        expected = SimpleNamespace(episodes={1: SimpleNamespace()})

        with patch("app.api.endpoints.subscribe.SubscribeChain") as subscribe_chain:
            subscribe_chain.return_value.subscribe_files_info.return_value = expected
            result = subscribe_files(
                subscribe_id=21,
                repository=repository,
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )

        assert result is expected
        subscribe_chain.return_value.subscribe_files_info.assert_called_once_with(own)

    def test_user_subscribes_hides_other_user_list(self):
        """
        普通用户不能通过 username 参数读取其他用户订阅列表。
        """
        from app.api.endpoints.subscribe import user_subscribes

        repository = _SubscriptionRepositoryFake(_EndpointSubscribe(id=20, username="bob"))
        result = asyncio.run(
            user_subscribes(
                username="bob",
                query=_subscription_query(repository),
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )

        assert result == []
        repository.async_list_by_username.assert_not_awaited()

    def test_async_add_subscribe_forwards_owner_scope_to_typed_writer(self):
        """
        owner-aware 创建应把当前用户交给 typed 写端口限定查重范围。
        """
        from app.application.subscription.write import async_add_subscribe

        writer = SimpleNamespace(async_add=AsyncMock(return_value=(22, "新增订阅成功")))
        sid, message = asyncio.run(
            async_add_subscribe(
                subscribe_oper=writer,
                mediainfo=_EndpointMediaInfo(),
                username="alice",
                owner_scope=True,
                season=1,
            )
        )

        assert sid == 22
        assert message == "新增订阅成功"
        writer.async_add.assert_awaited_once()
        call = writer.async_add.await_args.kwargs
        assert call["username"] == "alice"
        assert isinstance(call["payload"], SubscriptionPatch)

    def test_subscribe_history_scopes_regular_user_and_keeps_superuser_global(self):
        """
        订阅历史分页必须在 DB 层按 owner 收窄，避免全局页过滤后误判没有更多数据。
        """
        from app.api.endpoints.subscribe import subscribe_history

        own = _EndpointHistory(
            id=8,
            username="alice",
            name="自己的历史",
            type=MediaType.MOVIE.value,
        )
        other = _EndpointHistory(
            id=9,
            username="bob",
            name="他人的历史",
            type=MediaType.MOVIE.value,
        )
        legacy = _EndpointHistory(
            id=10,
            username="",
            name="旧历史",
            type=MediaType.MOVIE.value,
        )
        repository = _SubscriptionRepositoryFake()
        history_repository = _SubscriptionHistoryRepositoryFake(own, other, legacy)

        regular_result = asyncio.run(
            subscribe_history(
                mtype=MediaType.MOVIE.value,
                page=1,
                count=2,
                query=_subscription_query(repository, history_repository),
                current_user=_EndpointUser(name="alice", is_superuser=False),
                response=(regular_response := Response()),
            )
        )
        assert [history.id for history in regular_result] == [8]
        assert regular_response.headers["X-Total-Count"] == "1"
        history_repository.async_list_by_type_and_username.assert_awaited_once_with(
            MediaType.MOVIE.value,
            "alice",
            1,
            2,
        )
        history_repository.async_count_by_type_and_username.assert_awaited_once_with(
            MediaType.MOVIE.value,
            "alice",
        )
        history_repository.async_list_by_type.assert_not_awaited()

        history_repository.async_list_by_type_and_username.reset_mock()
        history_repository.async_count_by_type_and_username.reset_mock()
        history_repository.async_list_by_type.reset_mock()
        superuser_result = asyncio.run(
            subscribe_history(
                mtype=MediaType.MOVIE.value,
                page=1,
                count=3,
                query=_subscription_query(repository, history_repository),
                current_user=_EndpointUser(name="admin", is_superuser=True),
                response=(superuser_response := Response()),
            )
        )
        assert [history.id for history in superuser_result] == [8, 9, 10]
        assert superuser_response.headers["X-Total-Count"] == "3"
        history_repository.async_list_by_type.assert_awaited_once_with(
            MediaType.MOVIE.value,
            1,
            3,
        )
        history_repository.async_count_by_type.assert_awaited_once_with(MediaType.MOVIE.value)
        history_repository.async_list_by_type_and_username.assert_not_awaited()

    def test_delete_subscribe_history_rejects_other_user(self):
        """普通用户删除他人订阅历史时明确返回无权。"""
        from app.api.endpoints.subscribe import delete_subscribe_history

        other = _EndpointHistory(
            id=11,
            username="bob",
            name="他人的历史",
            type=MediaType.MOVIE.value,
        )
        repository = _SubscriptionRepositoryFake()
        history_repository = _SubscriptionHistoryRepositoryFake(other)

        with pytest.raises(HTTPException) as error:
            asyncio.run(
                delete_subscribe_history(
                    history_id=11,
                    mutation=_subscription_mutation(repository, history_repository),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        assert error.value.status_code == 403
        history_repository.stage_delete.assert_not_awaited()

    def test_global_refresh_and_check_require_superuser(self):
        """
        没有 owner 参数的全局订阅任务只允许超级用户触发。
        """
        from app.api.endpoints.subscribe import check_subscribes, refresh_subscribes

        regular_user = _EndpointUser(name="alice", is_superuser=False)
        superuser = _EndpointUser(name="admin", is_superuser=True)

        for endpoint in [refresh_subscribes, check_subscribes]:
            with patch("app.api.endpoints.submaintenance.get_scheduler") as scheduler:
                response = endpoint(current_user=regular_user)

            assert not response.success
            assert response.message == "订阅不存在"
            scheduler.return_value.start.assert_not_called()

        for endpoint, job_id in [
            (refresh_subscribes, "subscribe_refresh"),
            (check_subscribes, "subscribe_tmdb"),
        ]:
            with patch("app.api.endpoints.submaintenance.get_scheduler") as scheduler:
                response = endpoint(current_user=superuser)

            assert response.success
            scheduler.return_value.start.assert_called_once_with(job_id)

    def test_create_subscribe_excludes_system_fields_from_write_payload(self):
        """
        新增订阅时不应把历史 ID、媒体元数据和响应派生字段传入持久化链路。
        """
        subscribe_in = Subscribe(
            id=99,
            name="测试剧集",
            year="2026",
            type=MediaType.TV.value,
            season=1,
            poster="old-poster.jpg",
            backdrop="old-backdrop.jpg",
            vote=8.0,
            description="旧历史简介",
            total_episode=10,
            lack_episode=3,
        )

        assert subscribe_in.completed_episode == 7

        with patch(
            "app.api.endpoints.subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(1, "新增订阅成功")),
        ) as async_add:
            response = asyncio.run(
                create_subscribe(
                    subscribe_in=subscribe_in,
                    current_user=_EndpointUser(name="moviepilot-user", is_superuser=False),
                )
            )

        assert response.success
        payload = async_add.await_args.kwargs
        for field in ("id", "poster", "backdrop", "vote", "description", "completed_episode"):
            assert field not in payload
        assert payload["username"] == "moviepilot-user"
        assert payload["owner_scope"]

    def test_create_subscribe_ignores_runtime_fact_fields(self):
        """
        公共新增接口只能写目标和配置，调用方携带的运行事实不得进入新增链路。
        """
        subscribe_in = Subscribe(
            name="测试剧集",
            year="2026",
            type=MediaType.TV.value,
            season=1,
            total_episode=10,
            lack_episode=3,
            note=[1, 2, 3],
            state="S",
            last_update="2026-07-20 12:00:00",
            username="forged-user",
            current_priority=90,
            episode_priority={"1": 90},
            date="2026-07-19 12:00:00",
        )

        with patch(
            "app.api.endpoints.subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(1, "新增订阅成功")),
        ) as async_add:
            response = asyncio.run(
                create_subscribe(
                    subscribe_in=subscribe_in,
                    current_user=_EndpointUser(name="moviepilot-user", is_superuser=False),
                )
            )

        assert response.success
        payload = async_add.await_args.kwargs
        assert payload["username"] == "moviepilot-user"
        for field in (
            "lack_episode",
            "note",
            "state",
            "last_update",
            "current_priority",
            "episode_priority",
            "date",
            "completed_episode",
        ):
            assert field not in payload

    def test_create_subscribe_preserves_special_season_zero_with_douban_identity(self):
        """
        新增订阅带豆瓣 ID 且显式指定 S0 时，标题规整不应覆盖调用方传入的季号。
        """
        subscribe_in = Subscribe(
            name="测试剧集",
            year="2026",
            type=MediaType.TV.value,
            media_source=MediaSource.Douban,
            media_id="12345",
            season=0,
            total_episode=5,
            lack_episode=5,
        )

        with (
            patch(
                "app.api.endpoints.subscribe.MetaInfo",
                return_value=SimpleNamespace(name="测试剧集", begin_season=None),
            ),
            patch(
                "app.api.endpoints.subscribe.SubscribeChain.async_add",
                new=AsyncMock(return_value=(1, "新增订阅成功")),
            ) as async_add,
        ):
            response = asyncio.run(
                create_subscribe(
                    subscribe_in=subscribe_in,
                    current_user=_EndpointUser(name="moviepilot-user", is_superuser=False),
                )
            )

        assert response.success
        assert async_add.await_args.kwargs["season"] == 0
        assert async_add.await_args.kwargs["owner_scope"]

    def test_create_subscribe_keeps_superuser_global_deduplication(self):
        """
        超级用户新增订阅保持全局去重语义。
        """
        subscribe_in = Subscribe(
            name="测试电影",
            year="2026",
            type=MediaType.MOVIE.value,
        )

        with patch(
            "app.api.endpoints.subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(1, "订阅已存在")),
        ) as async_add:
            response = asyncio.run(
                create_subscribe(
                    subscribe_in=subscribe_in,
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        assert response.success
        assert not async_add.await_args.kwargs["owner_scope"]

    def test_update_status_sends_modified_event_payload_with_scene_and_fields(self):
        """
        状态更新只负责发出订阅修改事件，并携带场景和真实变更字段。
        """
        from app.api.endpoints.subscribe import update_subscribe_status

        subscribe = _EndpointSubscribe(id=5, state="R", name="测试订阅")
        repository = _SubscriptionRepositoryFake(subscribe)

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe_status(
                    subid=5,
                    state="S",
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        assert response.success
        send_event.assert_awaited_once()
        event_type, payload = send_event.await_args.args
        assert event_type == EventType.SubscribeModified
        assert payload["subscribe_id"] == 5
        assert payload["scene"] == "status"
        assert payload["fields"] == ["state"]
        assert payload["old_subscribe_info"]["state"] == "R"
        assert payload["subscribe_info"]["state"] == "S"

    def test_reset_sends_modified_event_payload_with_reset_scene(self):
        """
        reset 事件需要明确 scene，消费者不需要再从字段差异猜测用户意图。
        """
        from app.api.endpoints.subscribe import reset_subscribes

        subscribe = _EndpointSubscribe(
            id=6,
            state="S",
            name="测试订阅",
            total_episode=10,
            lack_episode=3,
            manual_total_episode=92,
            note=[1, 2],
            current_priority=80,
            episode_priority={"1": 80},
        )
        repository = _SubscriptionRepositoryFake(subscribe)

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                reset_subscribes(
                    subid=6,
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        assert response.success
        send_event.assert_awaited_once()
        event_type, payload = send_event.await_args.args
        assert event_type == EventType.SubscribeModified
        assert payload["subscribe_id"] == 6
        assert payload["scene"] == "reset"
        assert payload["fields"] == [
            "current_priority",
            "episode_priority",
            "lack_episode",
            "manual_total_episode",
            "note",
            "state",
        ]
        assert payload["subscribe_info"]["note"] == []
        assert payload["subscribe_info"]["lack_episode"] == 10
        assert payload["subscribe_info"]["manual_total_episode"] == 0

    def test_update_subscribe_sends_modified_event_payload_without_progress_refresh(self):
        """
        普通更新只发送 modify 事件；进度刷新由事件消费者或后续流程处理。
        """
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=7,
            name="旧标题",
            total_episode=8,
            lack_episode=2,
            vote=0.0,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        subscribe_in = Subscribe(id=7, name="新标题", total_episode=8, lack_episode=2)
        repository = _SubscriptionRepositoryFake(subscribe)

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        assert response.success
        send_event.assert_awaited_once()
        event_type, payload = send_event.await_args.args
        assert event_type == EventType.SubscribeModified
        assert payload["subscribe_id"] == 7
        assert payload["scene"] == "update"
        assert payload["fields"] == ["name"]
        assert payload["old_subscribe_info"]["name"] == "旧标题"
        assert payload["subscribe_info"]["name"] == "新标题"

    def test_update_subscribe_does_not_republish_pending_outbox_event(self):
        """服务返回 pending 时 endpoint 仍只返回业务成功，不得直接二次发布。"""
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=70,
            username="alice",
            name="旧标题",
            total_episode=8,
            lack_episode=2,
            sites=[],
            filter_groups=[],
            start_episode=0,
        )
        mutation = SimpleNamespace(
            get_accessible=AsyncMock(return_value=subscribe),
            update=AsyncMock(
                return_value=SimpleNamespace(
                    old=subscribe.to_dict(),
                    new={**subscribe.to_dict(), "name": "新标题"},
                    event_published=False,
                    business_committed=True,
                    pending_effects=("subscribe.modified:70:update:test:v1",),
                )
            ),
        )

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=Subscribe(
                        id=70,
                        name="新标题",
                        total_episode=8,
                        lack_episode=2,
                    ),
                    mutation=mutation,
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        assert response.success
        send_event.assert_not_awaited()

    def test_update_subscribe_ignores_runtime_fact_fields(self):
        """
        公共普通更新不得覆盖运行事实，状态调整继续由专用接口负责。
        """
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=8,
            username="alice",
            name="旧标题",
            total_episode=10,
            lack_episode=5,
            state="R",
            note=[1, 2, 3, 4, 5],
            current_priority=60,
            episode_priority={"1": 60},
            last_update="2026-07-19 12:00:00",
            date="2026-07-18 12:00:00",
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        subscribe_in = Subscribe(
            id=8,
            name="新标题",
            total_episode=10,
            lack_episode=0,
            state="S",
            note=[],
            current_priority=100,
            episode_priority={"1": 100},
            last_update="2026-07-20 12:00:00",
            date="2026-07-20 12:00:00",
        )
        repository = _SubscriptionRepositoryFake(subscribe)

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        assert response.success
        updated = repository.get(8)
        assert updated is not None
        assert updated.name == "新标题"
        assert updated.lack_episode == 5
        assert updated.state == "R"
        assert updated.note == [1, 2, 3, 4, 5]
        assert updated.current_priority == 60
        assert updated.episode_priority == {"1": 60}
        assert updated.last_update == "2026-07-19 12:00:00"
        assert updated.date == "2026-07-18 12:00:00"

    def test_update_subscribe_derives_lack_when_total_episode_increases(self):
        """
        公共更新扩大目标范围时，缺失集数与人工总集数标记仍由服务端派生。
        """
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=9,
            username="alice",
            name="测试剧集",
            total_episode=10,
            lack_episode=2,
            manual_total_episode=0,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        subscribe_in = Subscribe(id=9, name="测试剧集", total_episode=12, lack_episode=0)
        repository = _SubscriptionRepositoryFake(subscribe)

        with patch(
            "app.runtime.events.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    mutation=_subscription_mutation(repository),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        assert response.success
        updated = repository.get(9)
        assert updated is not None
        assert updated.total_episode == 12
        assert updated.lack_episode == 4
        assert updated.manual_total_episode == 1


class _EndpointUser(SimpleNamespace):
    """
    最小用户替身，模拟订阅 endpoint 依赖的用户权限字段。
    """

    def __init__(self, name: str, is_superuser: bool, permissions: dict | None = None):
        """保存 endpoint 权限判断依赖的最小用户字段。"""
        super().__init__(
            name=name,
            is_superuser=is_superuser,
            permissions=permissions or {},
        )


class _SubscriptionRepositoryFake:
    """以不可变订阅 DTO 模拟查询和修改仓储端口。"""

    def __init__(self, *rows: SubscriptionSnapshot) -> None:
        """保存初始快照并为调用断言暴露异步 mock。"""
        self.rows = {row.id: row for row in rows}
        self.async_get = AsyncMock(side_effect=self._async_get)
        self.async_list = AsyncMock(side_effect=self._async_list)
        self.async_list_by_username = AsyncMock(side_effect=self._async_list_by_username)
        self.async_list_by_media_identity = AsyncMock(side_effect=self._async_list_by_media_identity)
        self.async_list_by_title = AsyncMock(side_effect=self._async_list_by_title)
        self.async_update = AsyncMock(side_effect=self._async_update)
        self.async_stage_update = AsyncMock(side_effect=self._async_update)

    def get(self, subscribe_id: int) -> SubscriptionSnapshot | None:
        """同步按主键读取订阅快照。"""
        return self.rows.get(subscribe_id)

    async def _async_get(self, subscribe_id: int) -> SubscriptionSnapshot | None:
        """异步按主键读取订阅快照。"""
        return self.get(subscribe_id)

    async def _async_list(self, state: str | None = None) -> list[SubscriptionSnapshot]:
        """异步读取全部或指定状态的订阅快照。"""
        rows = list(self.rows.values())
        return [row for row in rows if state is None or row.state in state]

    async def _async_list_by_username(
        self,
        username: str,
        state: str | None = None,
        mtype: str | None = None,
    ) -> list[SubscriptionSnapshot]:
        """异步按用户及可选状态、类型读取订阅快照。"""
        return [
            row
            for row in self.rows.values()
            if row.username == username
            and (state is None or row.state in state)
            and (mtype is None or row.type == mtype)
        ]

    async def _async_list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: str | None = None,
    ) -> list[SubscriptionSnapshot]:
        """异步按规范媒体身份读取订阅快照。"""
        del music_type
        return [row for row in self.rows.values() if row.media_source == media_source and row.media_id == media_id]

    async def _async_list_by_title(
        self,
        title: str,
        season: int | None = None,
    ) -> list[SubscriptionSnapshot]:
        """异步按标题和季读取订阅快照。"""
        return [row for row in self.rows.values() if row.name == title and (season is None or row.season == season)]

    async def _async_update(
        self,
        subscribe_id: int,
        patch: SubscriptionPatch,
    ) -> SubscriptionSnapshot | None:
        """应用 typed patch 并保存新的不可变快照。"""
        row = self.rows.get(subscribe_id)
        if row is None:
            return None
        updated = replace(row, **patch.to_payload())
        self.rows[subscribe_id] = updated
        return updated


class _SubscriptionHistoryRepositoryFake:
    """以不可变历史 DTO 模拟历史查询和删除端口。"""

    def __init__(self, *rows: SubscriptionHistorySnapshot) -> None:
        """保存历史快照并为调用断言暴露异步 mock。"""
        self.rows = {row.id: row for row in rows}
        self.async_get = AsyncMock(side_effect=self._async_get)
        self.async_list_by_type = AsyncMock(side_effect=self._async_list_by_type)
        self.async_list_by_type_and_username = AsyncMock(side_effect=self._async_list_by_type_and_username)
        self.async_count_by_type = AsyncMock(side_effect=self._async_count_by_type)
        self.async_count_by_type_and_username = AsyncMock(side_effect=self._async_count_by_type_and_username)
        self.stage_delete = AsyncMock(side_effect=self._stage_delete)

    async def _async_get(self, history_id: int) -> SubscriptionHistorySnapshot | None:
        """异步按主键读取历史快照。"""
        return self.rows.get(history_id)

    async def _async_list_by_type(
        self,
        mtype: str,
        page: int = 1,
        count: int = 30,
    ) -> list[SubscriptionHistorySnapshot]:
        """异步按类型分页读取历史快照。"""
        rows = [row for row in self.rows.values() if row.type == mtype]
        start = (page - 1) * count
        return rows[start : start + count]

    async def _async_list_by_type_and_username(
        self,
        mtype: str,
        username: str,
        page: int = 1,
        count: int = 30,
    ) -> list[SubscriptionHistorySnapshot]:
        """异步按类型和用户分页读取历史快照。"""
        rows = [row for row in self.rows.values() if row.type == mtype and row.username == username]
        start = (page - 1) * count
        return rows[start : start + count]

    async def _async_count_by_type(self, mtype: str) -> int:
        """异步统计指定类型的历史快照。"""
        return sum(row.type == mtype for row in self.rows.values())

    async def _async_count_by_type_and_username(
        self,
        mtype: str,
        username: str,
    ) -> int:
        """异步统计指定类型和 owner 的历史快照。"""
        return sum(row.type == mtype and row.username == username for row in self.rows.values())

    async def _stage_delete(self, history_id: int) -> None:
        """暂存删除等价为从内存集合移除历史快照。"""
        self.rows.pop(history_id, None)


class _EndpointMediaInfo:
    """
    最小媒体信息替身，模拟 typed 写端口翻译所需字段。
    """

    title = "测试剧集"
    year = "2026"
    type = MediaType.TV
    media_source = MediaSource.TMDB
    media_id = "123"
    episode_group = None
    vote_average = 8.0
    overview = "测试简介"

    @staticmethod
    def get_poster_image():
        """返回固定海报地址。"""
        return "poster.jpg"

    @staticmethod
    def get_backdrop_image():
        """返回固定背景图地址。"""
        return "backdrop.jpg"


def _EndpointSubscribe(**kwargs: object) -> SubscriptionSnapshot:
    """从精简测试字段构造规范订阅快照。"""
    values = {"name": "测试订阅", **kwargs}
    return SubscriptionSnapshot(**values)


def _EndpointHistory(**kwargs: object) -> SubscriptionHistorySnapshot:
    """从精简测试字段构造规范订阅历史快照。"""
    values = {"name": "测试历史", **kwargs}
    return SubscriptionHistorySnapshot(**values)


def test_subscribe_accepts_empty_strings_for_numeric_fields():
    """前端提交音乐订阅时常以空字符串填充数值字段，不应触发 422。"""
    subscribe = Subscribe(
        name="Random Access Memories",
        type=MediaType.MUSIC.value,
        media_source="",
        media_id="",
        season="",
        total_episode="",
        start_episode="",
        best_version="",
        best_version_full="",
        current_priority="",
        search_imdbid="",
        vote="",
        episode_priority="",
        sites="",
        filter_groups="",
    )

    assert subscribe.media_source is None
    assert subscribe.media_id is None
    assert subscribe.season is None
    assert subscribe.best_version is None
    assert subscribe.episode_priority is None
    # 空字符串视为未提供，应回退到字段默认值而非 None
    assert subscribe.total_episode == 0
    assert subscribe.start_episode == 0
    assert subscribe.search_imdbid == 0
    assert subscribe.vote == 0.0
    assert subscribe.sites == []
    assert subscribe.filter_groups == []
    assert subscribe.type == MediaType.MUSIC.value


def test_subscribe_preserves_explicit_zero_and_numeric_string_values():
    """显式 0 和数字字符串应保持原有行为，不被空字符串归一化影响。"""
    subscribe = Subscribe(
        name="测试剧集",
        type=MediaType.TV.value,
        season="2",
        media_source=MediaSource.TMDB,
        media_id="123",
        total_episode=0,
        start_episode=0,
        search_imdbid=0,
        vote=0.0,
    )

    assert subscribe.season == 2
    assert subscribe.media_source == MediaSource.TMDB
    assert subscribe.media_id == "123"
    assert subscribe.total_episode == 0
    assert subscribe.start_episode == 0
    assert subscribe.search_imdbid == 0
    assert subscribe.vote == 0.0


@pytest.mark.parametrize(
    "identity",
    [
        {"media_source": MediaSource.TMDB},
        {"media_id": "123"},
        {"media_source": ""},
        {"media_id": ""},
        {"media_source": "invalid source:", "media_id": "123"},
        {"media_source": MediaSource.TMDB, "media_id": "0"},
        {"media_source": MediaSource.TMDB, "media_id": "   "},
    ],
)
def test_subscribe_schema_rejects_incomplete_or_invalid_media_identity(identity):
    """订阅 Schema 自身必须拒绝半对、零值和空白 ID，不能只依赖端点兜底。"""
    with pytest.raises(ValidationError):
        Subscribe(name="测试订阅", **identity)


def test_subscribe_schema_distinguishes_omitted_and_explicit_empty_identity():
    """省略和显式空对都合法，但必须保留字段是否由请求提交的信息。"""
    omitted = Subscribe(name="省略身份")
    explicit_empty = Subscribe(
        name="清空身份",
        media_source="",
        media_id="",
    )

    assert omitted.media_source is None
    assert omitted.media_id is None
    assert not {"media_source", "media_id"}.intersection(omitted.model_fields_set)
    assert explicit_empty.media_source is None
    assert explicit_empty.media_id is None
    assert {"media_source", "media_id"}.issubset(explicit_empty.model_fields_set)


def test_create_subscribe_accepts_music_payload_with_empty_strings():
    """带空字符串的音乐订阅应能通过新增订阅接口，不返回 422。"""
    subscribe_in = Subscribe(
        name="Random Access Memories",
        type=MediaType.MUSIC.value,
        music_type="album",
        total_tracks=13,
        media_source="",
        media_id="",
        season="",
        total_episode="",
        episode_priority="",
        sites="",
    )

    with patch(
        "app.api.endpoints.subscribe.SubscribeChain.async_add",
        new=AsyncMock(return_value=(1, "新增订阅成功")),
    ) as async_add:
        response = asyncio.run(
            create_subscribe(
                subscribe_in=subscribe_in,
                current_user=_EndpointUser(name="moviepilot-user", is_superuser=False),
            )
        )

    assert response.success is True
    payload = async_add.await_args.kwargs
    # 空字符串回退默认值后应正确传入持久化链路
    assert payload["media_source"] is None
    assert payload["media_id"] is None
    assert payload["total_episode"] == 0
    assert payload["sites"] == []
    assert payload["type"] == MediaType.MUSIC.value
    assert payload["music_type"] == "album"
    assert payload["total_tracks"] == 13


class _LegacyNoteRow:
    """携带历史字符串 note 的最小 ORM 替身。"""

    def __init__(self, note):
        """保存待规范化的历史 note 原值。"""
        self.note = note


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[1, 2, 3, 4]", [1, 2, 3, 4]),  # 双重 JSON 编码的整型数组
        ("[1, 2]", [1, 2]),
        ("[]", []),  # 双重编码的空数组
        ("null", None),  # 双重编码的 null
        ("not json", None),  # 无法解析的历史脏数据
        ([1, 2, 3], [1, 2, 3]),  # 正常列表原样保留
        (None, None),  # 空值
    ],
)
def test_subscribe_note_normalizes_legacy_json_string(raw, expected):
    """历史字符串型 note 应被解析为整数列表，避免响应校验 500。"""
    subscribe = Subscribe.model_validate(_LegacyNoteRow(note=raw))
    assert subscribe.note == expected


def test_subscribe_note_strips_non_int_items_from_legacy_string():
    """历史脏数据中混入非整数元素时只保留整数，不阻塞整个订阅列表接口。"""
    subscribe = Subscribe.model_validate(_LegacyNoteRow(note='[1, 2, "x", 3]'))
    assert subscribe.note == [1, 2, 3]
