import asyncio
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.api.endpoints.subscribe import create_subscribe
from app.application.subscription.mutation import SubscriptionMutationService
from app.application.subscription.query import SubscriptionQueryService
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.schemas.subscribe import Subscribe
from app.schemas.types import EventType, MediaSource, MediaType


def _subscription_query(db: object = None) -> SubscriptionQueryService:
    """构造使用测试数据库对象的订阅查询服务。"""
    return SubscriptionQueryService(
        repository=SubscribeOper(db),
        async_repository=SubscribeOper(db),
        history_repository=SubscribeHistoryOper(db),
    )


def _subscription_mutation(db: object = None) -> SubscriptionMutationService:
    """构造使用测试数据库对象的订阅写服务。"""
    return SubscriptionMutationService(
        repository=SubscribeOper(db),
        history_repository=SubscribeHistoryOper(db),
    )


class SubscribeEndpointTest(TestCase):
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

        with patch(
            "app.db.oper.subscribe.Subscribe.async_list",
            new=AsyncMock(return_value=all_subscribes),
        ), patch(
            "app.db.oper.subscribe.Subscribe.async_list_by_username",
            new=AsyncMock(return_value=[own]),
        ):
            api_token_result = asyncio.run(
                list_subscribes(query=_subscription_query(object()), _="api-token")
            )
            self.assertEqual([sub.id for sub in api_token_result], [1, 2, 3])

            regular_result = asyncio.run(
                read_subscribes(
                    query=_subscription_query(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )
            self.assertEqual([sub.id for sub in regular_result], [1])

            superuser_result = asyncio.run(
                read_subscribes(
                    query=_subscription_query(object()),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )
            self.assertEqual([sub.id for sub in superuser_result], [1, 2, 3])

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
            with self.subTest(subscribe_id=subscribe.id), patch(
                "app.db.oper.subscribe.Subscribe.async_get",
                new=AsyncMock(return_value=subscribe),
            ):
                result = asyncio.run(
                    read_subscribe(
                        subscribe_id=subscribe.id,
                        query=_subscription_query(object()),
                        current_user=current_user,
                    )
                )

            self.assertEqual(getattr(result, "id", None), expected_id)

    def test_delete_subscribe_delegates_identity_without_database_access(self):
        """按 ID 删除端点只映射用户身份，并保持不存在时也返回成功。"""
        from app.api.endpoints.subscribe import delete_subscribe

        command = SimpleNamespace(execute=AsyncMock(return_value=False))
        response = asyncio.run(
            delete_subscribe(
                subscribe_id=7,
                command=command,
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )

        self.assertTrue(response.success)
        command.execute.assert_awaited_once()
        subscribe_id, actor = command.execute.await_args.args
        self.assertEqual(subscribe_id, 7)
        self.assertEqual(actor.username, "alice")
        self.assertFalse(actor.is_superuser)

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
            with self.subTest(subscribe_id=subscribe.id), patch(
                "app.db.oper.subscribe.SubscribeOper.async_get",
                new=AsyncMock(return_value=subscribe),
            ), patch(
                "app.api.endpoints.subscribe.eventmanager.async_send_event",
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
                        mutation=_subscription_mutation(object()),
                        current_user=manage_user,
                    )
                )

            self.assertFalse(response.success)
            self.assertEqual(response.message, "订阅不存在")
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

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
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
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
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

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(subscribe.username, "alice")
        event_type, payload = send_event.await_args.args
        self.assertEqual(event_type, EventType.SubscribeModified)
        self.assertNotIn("username", payload["fields"])
        self.assertEqual(payload["subscribe_info"]["username"], "alice")

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

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=Subscribe(id=24, name="新标题"),
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(subscribe.media_source, MediaSource.TMDB)
        self.assertEqual(subscribe.media_id, "12345")

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

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
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
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        self.assertIsNone(subscribe.media_source)
        self.assertIsNone(subscribe.media_id)

    def test_update_subscribe_rejects_partial_media_identity(self):
        """更新媒体身份时只提交来源或 ID 之一应在 Schema 边界直接拒绝。"""
        with self.assertRaises(ValidationError):
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

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(subscribe.type, MediaType.MUSIC.value)
        self.assertEqual(subscribe.music_type, "album")
        self.assertEqual(subscribe.total_tracks, 11)

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
            with self.subTest(subscribe_id=subscribe.id), patch(
                "app.db.oper.subscribe.SubscribeOper.async_get",
                new=AsyncMock(side_effect=[subscribe, subscribe]),
            ), patch(
                "app.api.endpoints.subscribe.eventmanager.async_send_event",
                new=AsyncMock(),
            ) as send_event:
                response = asyncio.run(
                    update_subscribe_status(
                        subid=subscribe.id,
                        state="S",
                        mutation=_subscription_mutation(object()),
                        current_user=current_user,
                    )
                )

            self.assertTrue(response.success)
            send_event.assert_awaited_once()
            self.assertEqual(subscribe.state, "S")

    def test_share_subscribe_requires_local_owner(self):
        """
        分享本地订阅前必须确认当前用户有权读取该订阅行。
        """
        from app.api.endpoints.subscribe import subscribe_share
        from app.schemas.subscribe import SubscribeShare

        other = _EndpointSubscribe(id=7, username="bob", name="他人的订阅")

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(return_value=other),
        ), patch(
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
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertFalse(response.success)
        self.assertEqual(response.message, "订阅不存在")
        sub_share.assert_not_awaited()

    def test_subscribe_media_identity_returns_owner_when_other_candidate_matches_first(self):
        """
        按媒体查询订阅时，他人订阅不能挡住当前用户自己的订阅。
        """
        from app.api.endpoints.subscribe import subscribe_media_identity

        other = _EndpointSubscribe(
            id=13, username="bob", media_source="themoviedb", media_id="123", season=1
        )
        own = _EndpointSubscribe(
            id=14, username="alice", media_source="themoviedb", media_id="123", season=1
        )

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_list_by_media_identity",
            new=AsyncMock(return_value=[other, own]),
        ):
            result = asyncio.run(
                subscribe_media_identity(
                    media_id="123",
                    media_source=MediaSource.TMDB,
                    season=1,
                    query=_subscription_query(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertEqual(result.id, 14)

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

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_list_by_media_identity",
            new=AsyncMock(return_value=[recording, album]),
        ) as list_by_identity:
            result = asyncio.run(
                subscribe_media_identity(
                    media_id="shared-id",
                    media_source=MediaSource.MusicBrainz,
                    music_type="album",
                    query=_subscription_query(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertEqual(result.id, 22)
        self.assertEqual(list_by_identity.await_args.kwargs["music_type"], "album")

    def test_subscribe_media_identity_does_not_fallback_to_title(self):
        """统一身份未命中时不得按标题串联其他来源的订阅。"""
        from app.api.endpoints.subscribe import subscribe_media_identity

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_list_by_media_identity",
            new=AsyncMock(return_value=[]),
        ), patch(
            "app.db.oper.subscribe.SubscribeOper.async_list_by_title",
            new=AsyncMock(),
        ) as title_lookup:
            result = asyncio.run(
                subscribe_media_identity(
                    media_id="legacy-recording",
                    media_source=MediaSource.MusicBrainz,
                    title="周杰伦 - 晴天",
                    music_type="recording",
                    query=_subscription_query(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertIsNone(result.id)
        title_lookup.assert_not_awaited()

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

        self.assertTrue(response.success)
        command.execute.assert_awaited_once()
        media_source, media_id, season, music_type, actor = command.execute.await_args.args
        self.assertEqual(media_source, MediaSource.Douban)
        self.assertEqual(media_id, "douban-1")
        self.assertIsNone(season)
        self.assertIsNone(music_type)
        self.assertEqual(actor.username, "alice")
        self.assertFalse(actor.is_superuser)

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

        self.assertTrue(response.success)
        command.execute.assert_awaited_once()
        self.assertEqual(
            command.execute.await_args.args[:4],
            (
            MediaSource.MusicBrainz,
            "release-group-1",
            None,
            "album",
            ),
        )

    def test_search_subscribes_regular_user_schedules_only_owned_rows(self):
        """
        普通用户批量搜索把用户身份交给应用命令。
        """
        from app.api.endpoints.subscribe import search_subscribes

        command = SimpleNamespace(execute=AsyncMock(return_value=True))
        response = asyncio.run(
            search_subscribes(
                command=command,
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )
        )

        self.assertTrue(response.success)
        command.execute.assert_awaited_once()
        actor = command.execute.await_args.args[0]
        self.assertEqual(actor.username, "alice")
        self.assertFalse(actor.is_superuser)

    def test_subscribe_files_hides_other_user_row(self):
        """
        订阅文件接口不能向普通用户暴露他人的订阅文件信息。
        """
        from app.api.endpoints.subscribe import subscribe_files

        other = _EndpointSubscribe(id=19, username="bob", name="他人的订阅")

        with patch(
            "app.db.oper.subscribe.SubscribeOper.get",
            return_value=other,
        ), patch(
            "app.api.endpoints.subscribe.SubscribeChain"
        ) as subscribe_chain:
            result = subscribe_files(
                subscribe_id=19,
                mutation=_subscription_mutation(object()),
                current_user=_EndpointUser(name="alice", is_superuser=False),
            )

        self.assertEqual(result.episodes, {})
        subscribe_chain.return_value.subscribe_files_info.assert_not_called()

    def test_user_subscribes_hides_other_user_list(self):
        """
        普通用户不能通过 username 参数读取其他用户订阅列表。
        """
        from app.api.endpoints.subscribe import user_subscribes

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_list_by_username",
            new=AsyncMock(return_value=[_EndpointSubscribe(id=20, username="bob")]),
        ) as list_by_username:
            result = asyncio.run(
                user_subscribes(
                    username="bob",
                    query=_subscription_query(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertEqual(result, [])
        list_by_username.assert_not_awaited()

    def test_subscribe_oper_async_add_scopes_duplicate_lookup_by_owner(self):
        """
        owner-aware 创建不应把他人已有订阅当作当前用户订阅。
        """
        from app.application.subscription.write import async_add_subscribe
        from app.db.oper.subscribe import SubscribeOper

        other = _EndpointSubscribe(id=21, username="bob")
        own = _EndpointSubscribe(id=22, username="alice")
        created = SimpleNamespace(async_create=AsyncMock())
        session = SimpleNamespace(add=MagicMock(), flush=AsyncMock())

        with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
            subscribe_model.async_exists = AsyncMock(return_value=other)
            subscribe_model.async_exists_by_username = AsyncMock(
                side_effect=[None, own]
            )
            subscribe_model.return_value = created

            sid, message = asyncio.run(
                async_add_subscribe(
                    subscribe_oper=SubscribeOper(
                        db=session
                    ),
                    mediainfo=_EndpointMediaInfo(),
                    username="alice",
                    owner_scope=True,
                    season=1,
                )
            )

        self.assertEqual(sid, 22)
        self.assertEqual(message, "新增订阅成功")
        subscribe_model.async_exists.assert_not_awaited()
        self.assertEqual(subscribe_model.async_exists_by_username.await_count, 2)
        session.add.assert_called_once_with(created)
        session.flush.assert_awaited_once_with()

    def test_subscribe_history_scopes_regular_user_and_keeps_superuser_global(self):
        """
        订阅历史分页必须在 DB 层按 owner 收窄，避免全局页过滤后误判没有更多数据。
        """
        from app.api.endpoints.subscribe import subscribe_history

        own = _EndpointSubscribe(
            id=8,
            username="alice",
            name="自己的历史",
            type=MediaType.MOVIE.value,
        )
        other = _EndpointSubscribe(
            id=9,
            username="bob",
            name="他人的历史",
            type=MediaType.MOVIE.value,
        )
        legacy = _EndpointSubscribe(
            id=10,
            username="",
            name="旧历史",
            type=MediaType.MOVIE.value,
        )
        db = object()
        owner_query = AsyncMock(return_value=[own])
        global_query = AsyncMock(return_value=[other, legacy])

        with patch(
            "app.db.oper.subscribehistory.SubscribeHistoryOper.async_list_by_type",
            new=global_query,
        ), patch(
            "app.db.oper.subscribehistory.SubscribeHistoryOper.async_list_by_type_and_username",
            new=owner_query,
            create=True,
        ):
            regular_result = asyncio.run(
                subscribe_history(
                    mtype=MediaType.MOVIE.value,
                    page=1,
                    count=2,
                    query=_subscription_query(db),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )
            self.assertEqual([history.id for history in regular_result], [8])
            owner_query.assert_awaited_once_with(
                MediaType.MOVIE.value,
                "alice",
                1,
                2,
            )
            global_query.assert_not_awaited()

            owner_query.reset_mock()
            global_query.reset_mock(return_value=True)
            global_query.return_value = [own, other, legacy]

            superuser_result = asyncio.run(
                subscribe_history(
                    mtype=MediaType.MOVIE.value,
                    page=1,
                    count=3,
                    query=_subscription_query(db),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )
            self.assertEqual([history.id for history in superuser_result], [8, 9, 10])
            global_query.assert_awaited_once_with(
                MediaType.MOVIE.value,
                1,
                3,
            )
            owner_query.assert_not_awaited()

    def test_delete_subscribe_history_hides_other_from_regular_user(self):
        """
        普通用户删除他人订阅历史时按不存在处理。
        """
        from app.api.endpoints.subscribe import delete_subscribe_history

        other = _EndpointSubscribe(
            id=11,
            username="bob",
            name="他人的历史",
            type=MediaType.MOVIE.value,
        )

        with patch(
            "app.db.oper.subscribehistory.SubscribeHistoryOper.async_get",
            new=AsyncMock(return_value=other),
        ), patch(
            "app.db.oper.subscribehistory.SubscribeHistoryOper.async_delete",
            new=AsyncMock(),
        ) as async_delete:
            response = asyncio.run(
                delete_subscribe_history(
                    history_id=11,
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        async_delete.assert_not_awaited()

    def test_global_refresh_and_check_require_superuser(self):
        """
        没有 owner 参数的全局订阅任务只允许超级用户触发。
        """
        from app.api.endpoints.subscribe import check_subscribes, refresh_subscribes

        regular_user = _EndpointUser(name="alice", is_superuser=False)
        superuser = _EndpointUser(name="admin", is_superuser=True)

        for endpoint in [refresh_subscribes, check_subscribes]:
            with self.subTest(endpoint=endpoint.__name__), patch(
                "app.api.endpoints.subscribe.get_scheduler"
            ) as scheduler:
                response = endpoint(current_user=regular_user)

            self.assertFalse(response.success)
            self.assertEqual(response.message, "订阅不存在")
            scheduler.return_value.start.assert_not_called()

        for endpoint, job_id in [
            (refresh_subscribes, "subscribe_refresh"),
            (check_subscribes, "subscribe_tmdb"),
        ]:
            with self.subTest(endpoint=endpoint.__name__), patch(
                "app.api.endpoints.subscribe.get_scheduler"
            ) as scheduler:
                response = endpoint(current_user=superuser)

            self.assertTrue(response.success)
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

        self.assertEqual(subscribe_in.completed_episode, 7)

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

        self.assertTrue(response.success)
        payload = async_add.await_args.kwargs
        for field in ("id", "poster", "backdrop", "vote", "description", "completed_episode"):
            self.assertNotIn(field, payload)
        self.assertEqual(payload["username"], "moviepilot-user")
        self.assertTrue(payload["owner_scope"])

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

        self.assertTrue(response.success)
        payload = async_add.await_args.kwargs
        self.assertEqual(payload["username"], "moviepilot-user")
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
            self.assertNotIn(field, payload)

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

        with patch(
            "app.api.endpoints.subscribe.MetaInfo",
            return_value=SimpleNamespace(name="测试剧集", begin_season=None),
        ), patch(
            "app.api.endpoints.subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(1, "新增订阅成功")),
        ) as async_add:
            response = asyncio.run(
                create_subscribe(
                    subscribe_in=subscribe_in,
                    current_user=_EndpointUser(name="moviepilot-user", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(async_add.await_args.kwargs["season"], 0)
        self.assertTrue(async_add.await_args.kwargs["owner_scope"])

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

        self.assertTrue(response.success)
        self.assertFalse(async_add.await_args.kwargs["owner_scope"])

    def test_update_status_sends_modified_event_payload_with_scene_and_fields(self):
        """
        状态更新只负责发出订阅修改事件，并携带场景和真实变更字段。
        """
        from app.api.endpoints.subscribe import update_subscribe_status

        subscribe = _EndpointSubscribe(id=5, state="R", name="测试订阅")

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe_status(
                    subid=5,
                    state="S",
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        send_event.assert_awaited_once()
        event_type, payload = send_event.await_args.args
        self.assertEqual(event_type, EventType.SubscribeModified)
        self.assertEqual(payload["subscribe_id"], 5)
        self.assertEqual(payload["scene"], "status")
        self.assertEqual(payload["fields"], ["state"])
        self.assertEqual(payload["old_subscribe_info"]["state"], "R")
        self.assertEqual(payload["subscribe_info"]["state"], "S")

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

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                reset_subscribes(
                    subid=6,
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        send_event.assert_awaited_once()
        event_type, payload = send_event.await_args.args
        self.assertEqual(event_type, EventType.SubscribeModified)
        self.assertEqual(payload["subscribe_id"], 6)
        self.assertEqual(payload["scene"], "reset")
        self.assertEqual(
            payload["fields"],
            [
                "current_priority",
                "episode_priority",
                "lack_episode",
                "manual_total_episode",
                "note",
                "state",
            ],
        )
        self.assertEqual(payload["subscribe_info"]["note"], [])
        self.assertEqual(payload["subscribe_info"]["lack_episode"], 10)
        self.assertEqual(payload["subscribe_info"]["manual_total_episode"], 0)

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

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        send_event.assert_awaited_once()
        event_type, payload = send_event.await_args.args
        self.assertEqual(event_type, EventType.SubscribeModified)
        self.assertEqual(payload["subscribe_id"], 7)
        self.assertEqual(payload["scene"], "update")
        self.assertEqual(payload["fields"], ["name"])
        self.assertEqual(payload["old_subscribe_info"]["name"], "旧标题")
        self.assertEqual(payload["subscribe_info"]["name"], "新标题")

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

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(subscribe.name, "新标题")
        self.assertEqual(subscribe.lack_episode, 5)
        self.assertEqual(subscribe.state, "R")
        self.assertEqual(subscribe.note, [1, 2, 3, 4, 5])
        self.assertEqual(subscribe.current_priority, 60)
        self.assertEqual(subscribe.episode_priority, {"1": 60})
        self.assertEqual(subscribe.last_update, "2026-07-19 12:00:00")
        self.assertEqual(subscribe.date, "2026-07-18 12:00:00")

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

        with patch(
            "app.db.oper.subscribe.SubscribeOper.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    mutation=_subscription_mutation(object()),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(subscribe.total_episode, 12)
        self.assertEqual(subscribe.lack_episode, 4)
        self.assertEqual(subscribe.manual_total_episode, 1)


class _EndpointUser(SimpleNamespace):
    """
    最小用户替身，模拟订阅 endpoint 依赖的用户权限字段。
    """

    def __init__(self, name: str, is_superuser: bool, permissions: dict | None = None):
        super().__init__(
            name=name,
            is_superuser=is_superuser,
            permissions=permissions or {},
        )


class _EndpointAsyncDb:
    """
    最小异步数据库替身，用于观察 endpoint 删除的订阅对象。
    """

    def __init__(self):
        self.deleted = []
        self.committed = False
        self.rolled_back = False

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class _EndpointBackgroundTasks:
    """
    最小后台任务替身，记录 endpoint 入队的任务参数。
    """

    def __init__(self):
        self.tasks = []

    def add_task(self, func, **kwargs):
        self.tasks.append({"func": func, "kwargs": kwargs})


class _EndpointMediaInfo:
    """
    最小媒体信息替身，模拟 SubscribeOper 写订阅行所需字段。
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
        return "poster.jpg"

    @staticmethod
    def get_backdrop_image():
        return "backdrop.jpg"


class _EndpointSubscribe:
    """
    最小订阅替身，模拟 endpoint 依赖的 ORM 对象接口。
    """

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
        self.username = kwargs.pop("username", None)
        self.name = kwargs.pop("name", None)
        self.total_episode = kwargs.pop("total_episode", None)
        self.lack_episode = kwargs.pop("lack_episode", None)
        self.state = kwargs.pop("state", None)
        self.note = kwargs.pop("note", None)
        self.current_priority = kwargs.pop("current_priority", None)
        self.episode_priority = kwargs.pop("episode_priority", None)
        self.manual_total_episode = kwargs.pop("manual_total_episode", None)
        self.__dict__.update(kwargs)

    def to_dict(self):
        return {
            key: value
            for key, value in self.__dict__.items()
            if value is not None
        }

    async def async_update(self, _db, payload):
        self.__dict__.update(payload)


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
        self.note = note


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[1, 2, 3, 4]", [1, 2, 3, 4]),      # 双重 JSON 编码的整型数组
        ("[1, 2]", [1, 2]),
        ("[]", []),                          # 双重编码的空数组
        ("null", None),                      # 双重编码的 null
        ("not json", None),                  # 无法解析的历史脏数据
        ([1, 2, 3], [1, 2, 3]),              # 正常列表原样保留
        (None, None),                        # 空值
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
