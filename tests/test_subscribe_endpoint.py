import asyncio
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from app.api.endpoints.subscribe import create_subscribe
from app.schemas.subscribe import Subscribe
from app.schemas.types import EventType, MediaType


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
            "app.api.endpoints.subscribe.Subscribe.async_list",
            new=AsyncMock(return_value=all_subscribes),
        ), patch(
            "app.api.endpoints.subscribe.Subscribe.async_list_by_username",
            new=AsyncMock(return_value=[own]),
        ):
            api_token_result = asyncio.run(list_subscribes(_="api-token"))
            self.assertEqual([sub.id for sub in api_token_result], [1, 2, 3])

            regular_result = asyncio.run(
                read_subscribes(
                    db=object(),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )
            self.assertEqual([sub.id for sub in regular_result], [1])

            superuser_result = asyncio.run(
                read_subscribes(
                    db=object(),
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
                "app.api.endpoints.subscribe.Subscribe.async_get",
                new=AsyncMock(return_value=subscribe),
            ):
                result = asyncio.run(
                    read_subscribe(
                        subscribe_id=subscribe.id,
                        db=object(),
                        current_user=current_user,
                    )
                )

            self.assertEqual(getattr(result, "id", None), expected_id)

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
                "app.api.endpoints.subscribe.Subscribe.async_get",
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
                        db=object(),
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
            "app.api.endpoints.subscribe.Subscribe.async_get",
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
                    db=object(),
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
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    db=object(),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(subscribe.username, "alice")
        event_type, payload = send_event.await_args.args
        self.assertEqual(event_type, EventType.SubscribeModified)
        self.assertNotIn("username", payload["fields"])
        self.assertEqual(payload["subscribe_info"]["username"], "alice")

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
                "app.api.endpoints.subscribe.Subscribe.async_get",
                new=AsyncMock(side_effect=[subscribe, subscribe]),
            ), patch(
                "app.api.endpoints.subscribe.eventmanager.async_send_event",
                new=AsyncMock(),
            ) as send_event:
                response = asyncio.run(
                    update_subscribe_status(
                        subid=subscribe.id,
                        state="S",
                        db=object(),
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
            "app.api.endpoints.subscribe.Subscribe.async_get",
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
                    db=object(),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertFalse(response.success)
        self.assertEqual(response.message, "订阅不存在")
        sub_share.assert_not_awaited()

    def test_subscribe_mediaid_returns_owner_when_other_candidate_matches_first(self):
        """
        按媒体查询订阅时，他人订阅不能挡住当前用户自己的订阅。
        """
        from app.api.endpoints.subscribe import subscribe_mediaid

        other = _EndpointSubscribe(id=13, username="bob", tmdbid=123, season=1)
        own = _EndpointSubscribe(id=14, username="alice", tmdbid=123, season=1)

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_exists",
            new=AsyncMock(return_value=other),
        ), patch(
            "app.api.endpoints.subscribe.Subscribe.async_get_by_tmdbid",
            new=AsyncMock(return_value=[other, own]),
        ):
            result = asyncio.run(
                subscribe_mediaid(
                    mediaid="tmdb:123",
                    season=1,
                    db=object(),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertEqual(result.id, 14)

    def test_delete_subscribe_by_mediaid_deletes_owner_when_other_douban_match_first(self):
        """
        按媒体删除订阅时，应在候选集合中删除当前用户自己的订阅。
        """
        from app.api.endpoints.subscribe import delete_subscribe_by_mediaid

        other = _EndpointSubscribe(id=15, username="bob", doubanid="douban-1")
        own = _EndpointSubscribe(id=16, username="alice", doubanid="douban-1")
        db = _EndpointAsyncDb()

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get_by_doubanid",
            new=AsyncMock(return_value=other),
        ), patch(
            "app.api.endpoints.subscribe.Subscribe.async_list_by_doubanid",
            new=AsyncMock(return_value=[other, own]),
            create=True,
        ), patch(
            "app.api.endpoints.subscribe.build_subscribe_event_payload",
            return_value={"id": 16, "doubanid": "douban-1"},
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                delete_subscribe_by_mediaid(
                    mediaid="douban:douban-1",
                    db=db,
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(db.deleted, [own])
        send_event.assert_awaited_once()

    def test_search_subscribes_regular_user_schedules_only_owned_rows(self):
        """
        普通用户批量搜索只按自己的订阅 ID 入队。
        """
        from app.api.endpoints.subscribe import search_subscribes

        background_tasks = _EndpointBackgroundTasks()
        owned = [
            _EndpointSubscribe(id=17, username="alice", state="R"),
            _EndpointSubscribe(id=18, username="alice", state="R"),
        ]

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_list_by_username",
            new=AsyncMock(return_value=owned),
        ), patch("app.api.endpoints.subscribe.Scheduler") as scheduler_cls:
            response = asyncio.run(
                search_subscribes(
                    background_tasks=background_tasks,
                    db=object(),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(
            [task["kwargs"]["sid"] for task in background_tasks.tasks],
            [17, 18],
        )
        self.assertEqual(scheduler_cls.return_value.start.call_count, 0)

    def test_subscribe_files_hides_other_user_row(self):
        """
        订阅文件接口不能向普通用户暴露他人的订阅文件信息。
        """
        from app.api.endpoints.subscribe import subscribe_files

        other = _EndpointSubscribe(id=19, username="bob", name="他人的订阅")

        with patch(
            "app.api.endpoints.subscribe.Subscribe.get",
            return_value=other,
        ), patch(
            "app.api.endpoints.subscribe.SubscribeChain"
        ) as subscribe_chain:
            result = subscribe_files(
                subscribe_id=19,
                db=object(),
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
            "app.api.endpoints.subscribe.Subscribe.async_list_by_username",
            new=AsyncMock(return_value=[_EndpointSubscribe(id=20, username="bob")]),
        ) as list_by_username:
            result = asyncio.run(
                user_subscribes(
                    username="bob",
                    db=object(),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertEqual(result, [])
        list_by_username.assert_not_awaited()

    def test_subscribe_oper_async_add_scopes_duplicate_lookup_by_owner(self):
        """
        owner-aware 创建不应把他人已有订阅当作当前用户订阅。
        """
        from app.db.subscribe_oper import SubscribeOper

        other = _EndpointSubscribe(id=21, username="bob")
        own = _EndpointSubscribe(id=22, username="alice")
        created = SimpleNamespace(async_create=AsyncMock())

        with patch("app.db.subscribe_oper.Subscribe") as subscribe_model:
            subscribe_model.async_exists = AsyncMock(return_value=other)
            subscribe_model.async_exists_by_username = AsyncMock(
                side_effect=[None, own]
            )
            subscribe_model.return_value = created

            sid, message = asyncio.run(
                SubscribeOper(db=object()).async_add(
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
        created.async_create.assert_awaited_once()

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
            "app.api.endpoints.subscribe.SubscribeHistory.async_list_by_type",
            new=global_query,
        ), patch(
            "app.api.endpoints.subscribe.SubscribeHistory.async_list_by_type_and_username",
            new=owner_query,
            create=True,
        ):
            regular_result = asyncio.run(
                subscribe_history(
                    mtype=MediaType.MOVIE.value,
                    page=1,
                    count=2,
                    db=db,
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )
            self.assertEqual([history.id for history in regular_result], [8])
            owner_query.assert_awaited_once_with(
                db,
                mtype=MediaType.MOVIE.value,
                username="alice",
                page=1,
                count=2,
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
                    db=db,
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )
            self.assertEqual([history.id for history in superuser_result], [8, 9, 10])
            global_query.assert_awaited_once_with(
                db,
                mtype=MediaType.MOVIE.value,
                page=1,
                count=3,
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
            "app.api.endpoints.subscribe.SubscribeHistory.async_get",
            new=AsyncMock(return_value=other),
        ), patch(
            "app.api.endpoints.subscribe.SubscribeHistory.async_delete",
            new=AsyncMock(),
        ) as async_delete:
            response = asyncio.run(
                delete_subscribe_history(
                    history_id=11,
                    db=object(),
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
                "app.api.endpoints.subscribe.Scheduler"
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
                "app.api.endpoints.subscribe.Scheduler"
            ) as scheduler:
                response = endpoint(current_user=superuser)

            self.assertTrue(response.success)
            scheduler.return_value.start.assert_called_once_with(job_id)

    def test_create_subscribe_excludes_completed_episode_from_write_payload(self):
        """
        新增订阅时不应把 completed_episode 派生字段传入持久化链路。
        """
        subscribe_in = Subscribe(
            name="测试剧集",
            year="2026",
            type=MediaType.TV.value,
            season=1,
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
        self.assertNotIn("completed_episode", async_add.await_args.kwargs)
        self.assertEqual(async_add.await_args.kwargs["username"], "moviepilot-user")
        self.assertTrue(async_add.await_args.kwargs["owner_scope"])

    def test_create_subscribe_preserves_special_season_zero_with_doubanid(self):
        """
        新增订阅带豆瓣 ID 且显式指定 S0 时，标题规整不应覆盖调用方传入的季号。
        """
        subscribe_in = Subscribe(
            name="测试剧集",
            year="2026",
            type=MediaType.TV.value,
            doubanid="12345",
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
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe_status(
                    subid=5,
                    state="S",
                    db=object(),
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
            note=[1, 2],
            current_priority=80,
            episode_priority={"1": 80},
        )

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                reset_subscribes(
                    subid=6,
                    db=object(),
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
            ["current_priority", "episode_priority", "lack_episode", "note", "state"],
        )
        self.assertEqual(payload["subscribe_info"]["note"], [])
        self.assertEqual(payload["subscribe_info"]["lack_episode"], 10)

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
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    db=object(),
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
    tmdb_id = 123
    imdb_id = "tt123"
    tvdb_id = 456
    douban_id = "douban-1"
    bangumi_id = 789
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
