"""
验证插件评分提交、查询与统计结果处理。
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app import schemas
from app.adapters.external import server as server_module
from app.adapters.external.server import MoviePilotServerHelper
from app.api.endpoints.plugin import plugin_rating, plugin_ratings, rate_plugin
from app.application.plugin import rating as rating_module
from app.application.plugin.rating import PluginRatingService


def test_server_helper_uses_plugin_rating_endpoints() -> None:
    """
    评分辅助方法应使用独立中心端路径并传递评分载荷。
    """

    async def run_scenario() -> None:
        runtime_setting = server_module.get_runtime_setting
        with (
            patch.object(
                server_module,
                "get_runtime_setting",
                side_effect=lambda key: "https://movie-pilot.org" if key == "MP_SERVER_HOST" else runtime_setting(key),
            ),
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

        assert get_request.await_args_list[0].args == ("https://movie-pilot.org/plugin/rating",)
        assert get_request.await_args_list[0].kwargs == {
            "params": {"plugin_ids": "DemoPlugin,OtherPlugin"},
            "timeout": 10,
        }
        assert get_request.await_args_list[1].args == ("https://movie-pilot.org/plugin/rating/Demo%20Plugin",)
        assert post_request.await_args.args == (
            "https://movie-pilot.org/plugin/rating/Demo%20Plugin",
            {"rating": 4.5},
        )

    asyncio.run(run_scenario())


def test_plugin_rating_endpoints_return_center_results() -> None:
    """
    评分查询和提交接口应返回中心端结果并校验安装状态。
    """

    async def run_scenario() -> None:
        rating_result = {
            "plugin_id": "DemoPlugin",
            "average_rating": 4.3,
            "rating_count": 12,
            "user_rating": 4.5,
        }
        batch_query = AsyncMock(return_value={"DemoPlugin": rating_result})
        single_query = AsyncMock(return_value=rating_result)
        submit_rating = AsyncMock(return_value=rating_result)
        service = PluginRatingService(
            installed_plugins=lambda: ["DemoPlugin"],
            statistic=AsyncMock(return_value={}),
            ratings=batch_query,
            rating=single_query,
            submit=submit_rating,
        )
        with patch(
            "app.api.endpoints.plugin.get_plugin_rating_service",
            return_value=service,
        ):
            batch = await plugin_ratings("DemoPlugin", None)
            single = await plugin_rating("DemoPlugin", None)
            response = await rate_plugin(
                "DemoPlugin",
                schemas.PluginRatingRequest(rating=4.5),
                None,
            )

        assert batch["DemoPlugin"].average_rating == 4.3
        batch_query.assert_awaited_once_with(["DemoPlugin"])
        assert single.user_rating == 4.5
        assert response.success is True
        assert response.data == rating_result
        submit_rating.assert_awaited_once_with("DemoPlugin", 4.5)

    asyncio.run(run_scenario())


def test_plugin_rating_rejects_uninstalled_plugin() -> None:
    """
    未安装插件不能借助 MoviePilot 接口向中心端提交评分。
    """

    async def run_scenario() -> None:
        submit_rating = AsyncMock()
        service = PluginRatingService(
            installed_plugins=lambda: [],
            statistic=AsyncMock(return_value={}),
            ratings=AsyncMock(return_value={}),
            rating=AsyncMock(return_value={}),
            submit=submit_rating,
        )
        with patch(
            "app.api.endpoints.plugin.get_plugin_rating_service",
            return_value=service,
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


def test_plugin_rating_service_reset_isolates_lifespans(monkeypatch) -> None:
    """
    停机清理后不得继续复用上一 lifespan 的评分端口。
    """
    service = PluginRatingService(
        installed_plugins=lambda: [],
        statistic=AsyncMock(return_value={}),
        ratings=AsyncMock(return_value={}),
        rating=AsyncMock(return_value={}),
        submit=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(rating_module, "_rating_service", service)

    rating_module.reset_plugin_rating_service()

    with pytest.raises(RuntimeError, match="尚未完成初始化"):
        rating_module.get_plugin_rating_service()


@pytest.mark.asyncio
async def test_metrics_cache_expires_and_isolates_identity_and_callers(monkeypatch) -> None:
    """
    五分钟内同集合查询复用副本，缓存到期或中心端身份变化后重新读取。
    """
    clock = [0.0]
    scope = ["instance-one"]
    monkeypatch.setattr(rating_module, "monotonic", lambda: clock[0])
    ratings = AsyncMock(return_value={"Demo": {"average_rating": 4.0}})
    statistic = AsyncMock(return_value={"Demo": 20})
    service = PluginRatingService(
        installed_plugins=lambda: ["Demo"], statistic=statistic, ratings=ratings,
        rating=AsyncMock(), submit=AsyncMock(), cache_scope=lambda: scope[0],
    )
    first = await service.ratings(["Demo", "Other"])
    first["Demo"]["average_rating"] = 1
    assert (await service.ratings(["Other", "Demo", "Demo"]))["Demo"]["average_rating"] == 4
    ratings.assert_awaited_once()
    assert await service.statistic() == await service.statistic() == {"Demo": 20}
    statistic.assert_awaited_once()
    clock[0] = 301
    await service.ratings(["Demo", "Other"])
    await service.statistic()
    assert ratings.await_count == statistic.await_count == 2
    scope[0] = "instance-two"
    await service.ratings(["Demo", "Other"])
    await service.statistic()
    assert ratings.await_count == statistic.await_count == 3


@pytest.mark.asyncio
async def test_failed_metrics_are_not_cached_and_submit_invalidates_inflight_reads() -> None:
    """
    空失败结果立即允许重试，成功评分后在途旧结果不能再次写入缓存。
    """
    old_result = asyncio.Future()
    ratings = AsyncMock(side_effect=[{}, {"Demo": {"average_rating": 1.0}}])
    service = PluginRatingService(
        installed_plugins=lambda: ["Demo"], statistic=AsyncMock(return_value={}),
        ratings=ratings, rating=AsyncMock(), submit=AsyncMock(return_value={"average_rating": 5.0}),
    )
    assert await service.ratings(["Demo"]) == {}
    assert await service.ratings(["Demo"]) == {"Demo": {"average_rating": 1.0}}
    assert ratings.await_count == 2
    await service.submit("Demo", 5.0)

    async def pending_query(_ids):
        """
        保留一个评分提交前发出的查询，模拟中心端迟到的旧响应。
        """
        return await old_result

    ratings.side_effect = pending_query
    pending = asyncio.create_task(service.ratings(["Demo"]))
    await asyncio.sleep(0)
    await service.submit("Demo", 5.0)
    old_result.set_result({"Demo": {"average_rating": 1.0}})
    await pending
    ratings.side_effect = None
    ratings.return_value = {"Demo": {"average_rating": 5.0}}
    assert await service.ratings(["Demo"]) == ratings.return_value
    assert ratings.await_count == 4
