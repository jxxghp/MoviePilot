import asyncio
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from app.api.endpoints.subscribe import create_subscribe
from app.schemas.subscribe import Subscribe
from app.schemas.types import MediaType


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
