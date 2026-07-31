import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app import schemas
from app.api.endpoints.plugin import plugin_rating, plugin_ratings, rate_plugin
from app.helper.server import MoviePilotServerHelper


def test_server_helper_uses_plugin_rating_endpoints() -> None:
    """评分辅助方法应使用独立中心端路径并传递评分载荷。"""

    async def run_scenario() -> None:
        with (
            patch("app.helper.server.settings.MP_SERVER_HOST", "https://movie-pilot.org"),
            patch.object(
                MoviePilotServerHelper,
                "_async_get",
                new=AsyncMock(),
            ) as get_request,
            patch.object(
                MoviePilotServerHelper,
                "_async_post_json",
                new=AsyncMock(),
            ) as post_request,
        ):
            await MoviePilotServerHelper.async_plugin_ratings(["DemoPlugin", "OtherPlugin"])
            await MoviePilotServerHelper.async_plugin_rating("Demo Plugin")
            await MoviePilotServerHelper.async_rate_plugin("Demo Plugin", 4.5)

        assert get_request.await_args_list[0].args == (
            "https://movie-pilot.org/plugin/rating",
        )
        assert get_request.await_args_list[0].kwargs == {
            "params": {"plugin_ids": "DemoPlugin,OtherPlugin"},
            "timeout": 10,
        }
        assert get_request.await_args_list[1].args == (
            "https://movie-pilot.org/plugin/rating/Demo%20Plugin",
        )
        assert post_request.await_args.args == (
            "https://movie-pilot.org/plugin/rating/Demo%20Plugin",
            {"rating": 4.5},
        )

    asyncio.run(run_scenario())


def test_plugin_rating_endpoints_return_center_results() -> None:
    """评分查询和提交接口应返回中心端结果并校验安装状态。"""

    async def run_scenario() -> None:
        rating_result = {
            "plugin_id": "DemoPlugin",
            "average_rating": 4.3,
            "rating_count": 12,
            "user_rating": 4.5,
        }
        with patch.object(
            MoviePilotServerHelper,
            "async_get_plugin_ratings",
            new=AsyncMock(return_value={"DemoPlugin": rating_result}),
        ) as batch_query:
            batch = await plugin_ratings("DemoPlugin", None)

        assert batch["DemoPlugin"].average_rating == 4.3
        batch_query.assert_awaited_once_with(["DemoPlugin"])

        with patch.object(
            MoviePilotServerHelper,
            "async_get_plugin_rating",
            new=AsyncMock(return_value=rating_result),
        ):
            single = await plugin_rating("DemoPlugin", None)

        assert single.user_rating == 4.5

        system_config = MagicMock()
        system_config.get.return_value = ["DemoPlugin"]
        with (
            patch("app.api.endpoints.plugin.SystemConfigOper", return_value=system_config),
            patch.object(
                MoviePilotServerHelper,
                "async_submit_plugin_rating",
                new=AsyncMock(return_value=rating_result),
            ) as submit_rating,
        ):
            response = await rate_plugin(
                "DemoPlugin",
                schemas.PluginRatingRequest(rating=4.5),
                None,
            )

        assert response.success is True
        assert response.data == rating_result
        submit_rating.assert_awaited_once_with("DemoPlugin", 4.5)

    asyncio.run(run_scenario())


def test_plugin_rating_rejects_uninstalled_plugin() -> None:
    """未安装插件不能借助 MoviePilot 接口向中心端提交评分。"""

    async def run_scenario() -> None:
        system_config = MagicMock()
        system_config.get.return_value = []
        with (
            patch("app.api.endpoints.plugin.SystemConfigOper", return_value=system_config),
            patch.object(
                MoviePilotServerHelper,
                "async_submit_plugin_rating",
                new=AsyncMock(),
            ) as submit_rating,
        ):
            with pytest.raises(HTTPException) as error:
                await rate_plugin(
                    "DemoPlugin",
                    schemas.PluginRatingRequest(rating=4.5),
                    None,
                )

        assert error.value.status_code == 400
        submit_rating.assert_not_awaited()

    asyncio.run(run_scenario())
