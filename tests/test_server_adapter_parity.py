"""MoviePilot 中心服务适配器同步异步一致性测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.external import server as server_module
from app.adapters.external.server import MoviePilotServerHelper


@pytest.fixture(autouse=True)
def _reset_server_identity_cache():
    """隔离中心服务用户名缓存，避免同步入口短路异步入口。"""
    previous_user = MoviePilotServerHelper._github_user
    MoviePilotServerHelper._github_user = None
    yield
    MoviePilotServerHelper._github_user = previous_user


def _response(status_code: int, payload: object) -> SimpleNamespace:
    """构造不会触发真实网络的最小 HTTP 响应。"""
    return SimpleNamespace(status_code=status_code, json=Mock(return_value=payload))


def _settings(monkeypatch, **values) -> None:
    """只为当前用例注入中心服务配置。"""
    monkeypatch.setattr(
        server_module,
        "get_runtime_setting",
        lambda key: values.get(key),
    )


@pytest.mark.asyncio
async def test_github_user_sync_async_use_same_request_plan(monkeypatch) -> None:
    """GitHub 用户同步异步入口必须使用同一凭据、代理和响应映射。"""
    response = _response(200, {"login": "moviepilot"})
    sync_client = Mock()
    sync_client.get_res.return_value = response
    async_client = Mock()
    async_client.get_res = AsyncMock(return_value=response)
    sync_factory = Mock(return_value=sync_client)
    async_factory = Mock(return_value=async_client)
    _settings(
        monkeypatch,
        GITHUB_HEADERS={"Authorization": "token"},
        PROXY={"https": "http://proxy"},
    )
    monkeypatch.setattr(server_module, "RequestUtils", sync_factory)
    monkeypatch.setattr(server_module, "AsyncRequestUtils", async_factory)

    sync_result = MoviePilotServerHelper.get_github_user()
    MoviePilotServerHelper._github_user = None
    async_result = await MoviePilotServerHelper.async_get_github_user()

    assert sync_result == async_result == "moviepilot"
    assert sync_factory.call_args.kwargs == async_factory.call_args.kwargs == {
        "headers": {"Authorization": "token"},
        "proxies": {"https": "http://proxy"},
        "timeout": 15,
    }
    sync_client.get_res.assert_called_once_with("https://api.github.com/user")
    async_client.get_res.assert_awaited_once_with("https://api.github.com/user")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, {"is_admin": True}, {"is_admin": True}),
        (403, {"message": "forbidden"}, {}),
    ],
)
async def test_user_permissions_sync_async_classify_response_identically(
    monkeypatch,
    status_code: int,
    payload: dict,
    expected: dict,
) -> None:
    """用户权限同步异步入口必须共享 HTTP 状态和对象响应分类。"""
    response = _response(status_code, payload)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "get_github_user",
        Mock(return_value="moviepilot"),
    )
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_get_github_user",
        AsyncMock(return_value="moviepilot"),
    )
    sync_request = Mock(return_value=response)
    async_request = AsyncMock(return_value=response)
    monkeypatch.setattr(MoviePilotServerHelper, "user_permissions", sync_request)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_user_permissions",
        async_request,
    )

    sync_result = MoviePilotServerHelper.get_user_permissions()
    async_result = await MoviePilotServerHelper.async_get_user_permissions()

    assert sync_result == async_result == expected
    sync_request.assert_called_once_with("moviepilot")
    async_request.assert_awaited_once_with("moviepilot")


@pytest.mark.asyncio
async def test_user_permissions_sync_async_map_transport_failure_identically(
    monkeypatch,
) -> None:
    """用户权限同步异步传输异常都必须回退为空权限。"""
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "get_github_user",
        Mock(return_value="moviepilot"),
    )
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_get_github_user",
        AsyncMock(return_value="moviepilot"),
    )
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "user_permissions",
        Mock(side_effect=RuntimeError("offline")),
    )
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_user_permissions",
        AsyncMock(side_effect=RuntimeError("offline")),
    )

    assert MoviePilotServerHelper.get_user_permissions() == {}
    assert await MoviePilotServerHelper.async_get_user_permissions() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "user_uid", "status_code", "expected"),
    [
        (False, "uid-1", 200, False),
        (True, "", 200, False),
        (True, "uid-1", 500, False),
        (True, "uid-1", 200, True),
    ],
)
async def test_usage_report_sync_async_share_preflight_and_result_mapping(
    monkeypatch,
    enabled: bool,
    user_uid: str,
    status_code: int,
    expected: bool,
) -> None:
    """版本统计开关、实例身份和成功状态必须由同一计划解释。"""
    payload = {"user_uid": user_uid, "backend_version": "3.0.0"}
    response = _response(status_code, {})
    _settings(monkeypatch, USAGE_STATISTIC_SHARE=enabled)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "build_usage_payload",
        Mock(return_value=payload),
    )
    sync_request = Mock(return_value=response)
    async_request = AsyncMock(return_value=response)
    monkeypatch.setattr(MoviePilotServerHelper, "usage_report", sync_request)
    monkeypatch.setattr(MoviePilotServerHelper, "async_usage_report", async_request)

    sync_result = MoviePilotServerHelper.report_usage()
    async_result = await MoviePilotServerHelper.async_report_usage()

    assert sync_result == async_result == expected
    if enabled and user_uid:
        sync_request.assert_called_once_with(payload)
        async_request.assert_awaited_once_with(payload)
    else:
        sync_request.assert_not_called()
        async_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_usage_report_sync_async_map_transport_failure_identically(
    monkeypatch,
) -> None:
    """版本统计同步异步传输异常都必须映射为上报失败。"""
    _settings(monkeypatch, USAGE_STATISTIC_SHARE=True)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "build_usage_payload",
        Mock(return_value={"user_uid": "uid-1"}),
    )
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "usage_report",
        Mock(side_effect=RuntimeError("offline")),
    )
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_usage_report",
        AsyncMock(side_effect=RuntimeError("offline")),
    )

    assert MoviePilotServerHelper.report_usage() is False
    assert await MoviePilotServerHelper.async_report_usage() is False


@pytest.mark.asyncio
async def test_plugin_install_sync_async_share_payload_and_result(monkeypatch) -> None:
    """插件安装统计同步异步入口必须使用同一脱敏载荷和成功分类。"""
    response = _response(200, {})
    _settings(monkeypatch, PLUGIN_STATISTIC_SHARE=True)
    sync_request = Mock(return_value=response)
    async_request = AsyncMock(return_value=response)
    monkeypatch.setattr(MoviePilotServerHelper, "plugin_install", sync_request)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_plugin_install",
        async_request,
    )

    sync_result = MoviePilotServerHelper.install_plugin_reg(
        "DemoPlugin",
        "local://DemoPlugin?version=v3",
    )
    async_result = await MoviePilotServerHelper.async_install_plugin_reg(
        "DemoPlugin",
        "local://DemoPlugin?version=v3",
    )

    expected_payload = {
        "plugin_id": "DemoPlugin",
        "repo_url": "local://DemoPlugin?version=v3",
    }
    assert sync_result == async_result is True
    sync_request.assert_called_once_with("DemoPlugin", expected_payload)
    async_request.assert_awaited_once_with("DemoPlugin", expected_payload)


@pytest.mark.asyncio
async def test_plugin_statistic_sync_async_share_response_classification(
    monkeypatch,
) -> None:
    """插件统计同步异步入口必须共享功能开关和对象响应分类。"""
    response = _response(200, {"DemoPlugin": 3})
    _settings(monkeypatch, PLUGIN_STATISTIC_SHARE=True)
    sync_request = Mock(return_value=response)
    async_request = AsyncMock(return_value=response)
    monkeypatch.setattr(MoviePilotServerHelper, "plugin_statistic", sync_request)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_plugin_statistic",
        async_request,
    )
    MoviePilotServerHelper.get_plugin_statistic.cache_clear()

    sync_result = MoviePilotServerHelper.get_plugin_statistic()
    async_result = await MoviePilotServerHelper.async_get_plugin_statistic()

    assert sync_result == async_result == {"DemoPlugin": 3}
    sync_request.assert_called_once_with()
    async_request.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sync_name", "async_name", "sync_transport", "async_transport"),
    [
        ("sub_reg", "async_sub_reg", "subscribe_add", "async_subscribe_add"),
        ("sub_done", "async_sub_done", "subscribe_done", "async_subscribe_done"),
    ],
)
async def test_subscribe_statistic_sync_async_share_payload_and_result(
    monkeypatch,
    sync_name: str,
    async_name: str,
    sync_transport: str,
    async_transport: str,
) -> None:
    """订阅新增与完成统计必须共享功能开关、载荷生成和成功分类。"""
    response = _response(200, {})
    payload = {"media_source": "tmdb", "media_id": "1"}
    _settings(monkeypatch, SUBSCRIBE_STATISTIC_SHARE=True)
    payload_builder = Mock(return_value=payload)
    sync_request = Mock(return_value=response)
    async_request = AsyncMock(return_value=response)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "_build_subscribe_statistic_payload",
        payload_builder,
    )
    monkeypatch.setattr(MoviePilotServerHelper, sync_transport, sync_request)
    monkeypatch.setattr(MoviePilotServerHelper, async_transport, async_request)

    sync_result = getattr(MoviePilotServerHelper, sync_name)({"id": 1})
    async_result = await getattr(MoviePilotServerHelper, async_name)({"id": 1})

    assert sync_result == async_result is True
    assert payload_builder.call_count == 2
    sync_request.assert_called_once_with(payload)
    async_request.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_subscribe_list_sync_async_share_query_and_result(monkeypatch) -> None:
    """订阅分享列表同步异步入口必须共享筛选参数和列表响应分类。"""
    response = _response(200, [{"id": 1}])
    _settings(monkeypatch, SUBSCRIBE_STATISTIC_SHARE=True)
    sync_request = Mock(return_value=response)
    async_request = AsyncMock(return_value=response)
    monkeypatch.setattr(MoviePilotServerHelper, "subscribe_shares", sync_request)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_subscribe_shares",
        async_request,
    )
    MoviePilotServerHelper.get_subscribe_shares.cache_clear()
    await MoviePilotServerHelper.async_get_subscribe_shares.cache_clear()

    sync_result = MoviePilotServerHelper.get_subscribe_shares(
        name="Demo",
        page=2,
        count=10,
        genre_id=16,
        min_rating=7.0,
        sort_type="rating",
    )
    await MoviePilotServerHelper.async_get_subscribe_shares.cache_clear()
    async_result = await MoviePilotServerHelper.async_get_subscribe_shares(
        name="Demo",
        page=2,
        count=10,
        genre_id=16,
        min_rating=7.0,
        sort_type="rating",
    )

    expected_params = {
        "page": 2,
        "count": 10,
        "name": "Demo",
        "genre_id": 16,
        "min_rating": 7.0,
        "sort_type": "rating",
    }
    assert sync_result == async_result == [{"id": 1}]
    sync_request.assert_called_once_with(expected_params)
    async_request.assert_awaited_once_with(expected_params)


@pytest.mark.asyncio
async def test_workflow_list_sync_async_share_query_and_result(monkeypatch) -> None:
    """工作流分享列表同步异步入口必须共享开关、分页计划和结果分类。"""
    response = _response(200, [{"id": 2}])
    _settings(monkeypatch, WORKFLOW_STATISTIC_SHARE=True)
    sync_request = Mock(return_value=response)
    async_request = AsyncMock(return_value=response)
    monkeypatch.setattr(MoviePilotServerHelper, "workflow_shares", sync_request)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_workflow_shares",
        async_request,
    )
    MoviePilotServerHelper.get_workflow_shares.cache_clear()
    await MoviePilotServerHelper.async_get_workflow_shares.cache_clear()

    sync_result = MoviePilotServerHelper.get_workflow_shares("Demo", 3, 20)
    await MoviePilotServerHelper.async_get_workflow_shares.cache_clear()
    async_result = await MoviePilotServerHelper.async_get_workflow_shares(
        "Demo", 3, 20
    )

    expected_params = {"name": "Demo", "page": 3, "count": 20}
    assert sync_result == async_result == [{"id": 2}]
    sync_request.assert_called_once_with(expected_params)
    async_request.assert_awaited_once_with(expected_params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_key", "sync_name", "async_name", "message"),
    [
        (
            "SUBSCRIBE_STATISTIC_SHARE",
            "share_delete",
            "async_share_delete",
            "当前没有开启订阅数据共享功能",
        ),
        (
            "WORKFLOW_STATISTIC_SHARE",
            "workflow_share_delete_by_id",
            "async_workflow_share_delete_by_id",
            "当前没有开启工作流数据共享功能",
        ),
    ],
)
async def test_share_delete_sync_async_use_same_disabled_result(
    monkeypatch,
    setting_key: str,
    sync_name: str,
    async_name: str,
    message: str,
) -> None:
    """订阅与工作流删除入口必须共享禁用状态和失败文案。"""
    _settings(monkeypatch, **{setting_key: False})

    sync_result = getattr(MoviePilotServerHelper, sync_name)(9)
    async_result = await getattr(MoviePilotServerHelper, async_name)(9)

    assert sync_result == async_result == (False, message)


@pytest.mark.asyncio
async def test_recognize_query_sync_async_share_plan_and_result(monkeypatch) -> None:
    """共享识别查询必须共用启用判断、查询计划和业务响应解析。"""
    response = _response(200, {"code": 0, "data": {"item": {"media_id": "1"}}})
    params = {"keyword": "Demo", "type": "movie"}
    _settings(monkeypatch, MEDIA_RECOGNIZE_SHARE=True)
    builder = Mock(return_value=params)
    sync_request = Mock(return_value=response)
    async_request = AsyncMock(return_value=response)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "_build_recognize_query_params",
        builder,
    )
    monkeypatch.setattr(MoviePilotServerHelper, "recognize_query", sync_request)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_recognize_query",
        async_request,
    )

    sync_result = MoviePilotServerHelper.query_recognize_share(None)
    async_result = await MoviePilotServerHelper.async_query_recognize_share(None)

    assert sync_result == async_result == {"media_id": "1"}
    assert builder.call_count == 2
    sync_request.assert_called_once_with(params)
    async_request.assert_awaited_once_with(params)


@pytest.mark.asyncio
async def test_recognize_report_sync_async_share_plan_and_result(monkeypatch) -> None:
    """共享识别上报必须共用启用判断、载荷计划和业务成功分类。"""
    response = _response(200, {"code": 0})
    payload = {"keyword": "Demo", "media_source": "tmdb", "media_id": "1"}
    _settings(monkeypatch, MEDIA_RECOGNIZE_SHARE=True)
    builder = Mock(return_value=payload)
    sync_request = Mock(return_value=response)
    async_request = AsyncMock(return_value=response)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "_build_recognize_report_payload",
        builder,
    )
    monkeypatch.setattr(MoviePilotServerHelper, "recognize_report", sync_request)
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_recognize_report",
        async_request,
    )

    sync_result = MoviePilotServerHelper.report_recognize_share(None, None)
    async_result = await MoviePilotServerHelper.async_report_recognize_share(None, None)

    assert sync_result == async_result is True
    assert builder.call_count == 2
    sync_request.assert_called_once_with(payload)
    async_request.assert_awaited_once_with(payload)
