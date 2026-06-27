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
                    current_user=SimpleNamespace(name="moviepilot-user"),
                )
            )

        self.assertTrue(response.success)
        self.assertNotIn("completed_episode", async_add.await_args.kwargs)
        self.assertEqual(async_add.await_args.kwargs["username"], "moviepilot-user")

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
                    current_user=SimpleNamespace(name="moviepilot-user"),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(async_add.await_args.kwargs["season"], 0)

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
            response = asyncio.run(update_subscribe_status(subid=5, state="S", db=object()))

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
            response = asyncio.run(reset_subscribes(subid=6, db=object()))

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
            response = asyncio.run(update_subscribe(subscribe_in=subscribe_in, db=object()))

        self.assertTrue(response.success)
        send_event.assert_awaited_once()
        event_type, payload = send_event.await_args.args
        self.assertEqual(event_type, EventType.SubscribeModified)
        self.assertEqual(payload["subscribe_id"], 7)
        self.assertEqual(payload["scene"], "update")
        self.assertEqual(payload["fields"], ["name"])
        self.assertEqual(payload["old_subscribe_info"]["name"], "旧标题")
        self.assertEqual(payload["subscribe_info"]["name"], "新标题")


class _EndpointSubscribe:
    """
    最小订阅替身，模拟 endpoint 依赖的 ORM 对象接口。
    """

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
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
