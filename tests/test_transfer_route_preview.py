from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api.endpoints import transfer as transfer_endpoint
from app.chain.directory_route import DirectoryRouteChain
from app.db.user_oper import (
    get_current_active_manage_user,
    get_current_active_superuser_async,
)
from app.schemas import (
    CategoryConfig,
    CategoryRule,
    TransferDirectoryConf,
    DirectoryRouteSettings,
    TransferRouteMediaSnapshot,
    TransferRoutePreviewRequest,
)
from app.schemas.types import DirectoryMatchMode, EventType, MediaType, SystemConfigKey


def _preview_request(category: str | None = None) -> TransferRoutePreviewRequest:
    """构造无外部依赖的目录路由预览请求。"""
    return TransferRoutePreviewRequest(
        media=TransferRouteMediaSnapshot(
            type=MediaType.TV,
            title="测试综艺",
            year="2026",
            category=category,
        ),
        metadata={"genre_ids": [10764], "origin_country": ["CN"]},
        category_config=CategoryConfig(
            tv={"综艺": CategoryRule(genre_ids="10764")},
        ),
        directories=[
            TransferDirectoryConf(
                name="通用电视剧",
                media_type=MediaType.TV.value,
                monitor_type="monitor",
                storage="local",
                library_storage="local",
                download_path="/downloads",
                library_path="/library/tv",
            ),
            TransferDirectoryConf(
                name="综艺",
                media_type=MediaType.TV.value,
                media_category="综艺",
                monitor_type="monitor",
                storage="local",
                library_storage="local",
                download_path="/downloads",
                library_path="/library/variety",
            ),
        ],
        match_mode=DirectoryMatchMode.SPECIFICITY,
    )


def test_directory_route_chain_preview_compares_modes_without_network() -> None:
    """路由预览应复用同一快照比较两种模式且不触发外部识别。"""
    response = DirectoryRouteChain.preview(_preview_request())

    assert response.category.selected_category == "综艺"
    assert response.metadata["genre_ids"] == [10764]
    assert response.category.source == "automatic"
    assert response.route.mode == DirectoryMatchMode.SPECIFICITY
    assert response.route.selected_directory.name == "综艺"
    decisions = {decision.mode: decision for decision in response.comparisons}
    assert decisions[DirectoryMatchMode.SEQUENTIAL].selected_directory.name == "通用电视剧"
    assert decisions[DirectoryMatchMode.SPECIFICITY].selected_directory.name == "综艺"


def test_directory_route_chain_preview_preserves_provided_category_and_warns_on_conflict() -> None:
    """订阅或历史提供的类别优先于自动分类，并明确报告冲突。"""
    response = DirectoryRouteChain.preview(_preview_request(category="纪录片"))

    assert response.category.automatic_category == "综艺"
    assert response.category.selected_category == "纪录片"
    assert response.category.source == "provided"
    assert any(warning.code == "provided_category_conflict" for warning in response.category.warnings)


def test_directory_route_chain_preview_reads_current_configs_when_drafts_are_omitted(monkeypatch) -> None:
    """未传草稿时只读取本地配置，不调用媒体识别或 TMDB。"""
    request = _preview_request()
    request.category_config = None
    request.directories = None
    category_config = CategoryConfig(tv={"综艺": CategoryRule(genre_ids="10764")})
    directories = _preview_request().directories
    monkeypatch.setattr(
        "app.chain.directory_route.CategoryHelper.load",
        lambda _self: category_config,
    )
    monkeypatch.setattr(
        "app.chain.directory_route.DirectoryHelper.get_dirs",
        lambda _self: directories,
    )

    response = DirectoryRouteChain.preview(request)

    assert response.route.selected_directory.name == "综艺"


@pytest.mark.anyio
async def test_route_preview_endpoint_uses_typed_chain_contract() -> None:
    """REST 入口应校验请求并把业务编排交给 DirectoryRouteChain。"""
    request = _preview_request()
    expected = DirectoryRouteChain.preview(request)
    app = FastAPI()
    app.include_router(transfer_endpoint.router, prefix="/api/v1/transfer")
    app.dependency_overrides[get_current_active_manage_user] = lambda: Mock()

    with patch.object(DirectoryRouteChain, "preview", return_value=expected) as preview:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/transfer/route/preview",
                json=request.model_dump(mode="json"),
            )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["route"]["selected_directory"]["name"] == "综艺"
    preview.assert_called_once()
    assert preview.call_args.args[0].media.type == MediaType.TV


@pytest.mark.anyio
async def test_route_settings_endpoint_saves_one_typed_contract() -> None:
    """目录和匹配模式应通过同一后端契约保存。"""
    route_settings = DirectoryRouteSettings(
        directories=_preview_request().directories,
        match_mode=DirectoryMatchMode.SPECIFICITY,
    )
    app = FastAPI()
    app.include_router(transfer_endpoint.router, prefix="/api/v1/transfer")
    app.dependency_overrides[get_current_active_superuser_async] = lambda: Mock()

    with patch.object(
        DirectoryRouteChain,
        "save_settings",
        new=AsyncMock(return_value=route_settings),
    ) as save_settings:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/transfer/route/settings",
                json=route_settings.model_dump(mode="json"),
            )

    assert response.status_code == 200
    assert response.json()["data"]["match_mode"] == "specificity"
    save_settings.assert_awaited_once()
    assert save_settings.await_args.args[0] == route_settings


@pytest.mark.anyio
async def test_directory_route_chain_saves_settings_atomically(monkeypatch) -> None:
    """业务链应批量持久化两个配置键并只广播一次变更。"""
    route_settings = DirectoryRouteSettings(
        directories=_preview_request().directories,
        match_mode=DirectoryMatchMode.SPECIFICITY,
    )
    config_oper = Mock()
    config_oper.async_set_many = AsyncMock(return_value={
        SystemConfigKey.Directories.value,
        SystemConfigKey.DirectoryMatchMode.value,
    })
    send_event = AsyncMock()
    monkeypatch.setattr("app.chain.directory_route.SystemConfigOper", lambda: config_oper)
    monkeypatch.setattr("app.chain.directory_route.eventmanager.async_send_event", send_event)

    saved = await DirectoryRouteChain.save_settings(route_settings)

    assert saved == route_settings
    values = config_oper.async_set_many.await_args.args[0]
    assert values[SystemConfigKey.DirectoryMatchMode] == "specificity"
    assert values[SystemConfigKey.Directories][1]["media_category"] == "综艺"
    send_event.assert_awaited_once()
    assert send_event.await_args.kwargs["etype"] is EventType.ConfigChanged
    assert send_event.await_args.kwargs["data"].key == {
        SystemConfigKey.Directories.value,
        SystemConfigKey.DirectoryMatchMode.value,
    }


def test_directory_route_chain_reads_settings_from_one_snapshot(monkeypatch) -> None:
    """目录设置读取应通过一次多键快照构造完整响应。"""
    config_oper = Mock()
    config_oper.get_many.return_value = {
        SystemConfigKey.Directories.value: [
            directory.model_dump(mode="json", exclude_none=True)
            for directory in _preview_request().directories
        ],
        SystemConfigKey.DirectoryMatchMode.value: "specificity",
    }
    monkeypatch.setattr("app.chain.directory_route.SystemConfigOper", lambda: config_oper)

    settings = DirectoryRouteChain.get_settings()

    assert settings.match_mode is DirectoryMatchMode.SPECIFICITY
    assert settings.directories[1].name == "综艺"
    config_oper.get_many.assert_called_once_with((
        SystemConfigKey.Directories,
        SystemConfigKey.DirectoryMatchMode,
    ))


@pytest.mark.anyio
async def test_route_settings_endpoint_returns_current_atomic_contract() -> None:
    """设置查询入口应一次返回目录和匹配模式。"""
    route_settings = DirectoryRouteSettings(
        directories=_preview_request().directories,
        match_mode=DirectoryMatchMode.SPECIFICITY,
    )
    app = FastAPI()
    app.include_router(transfer_endpoint.router, prefix="/api/v1/transfer")
    app.dependency_overrides[get_current_active_manage_user] = lambda: Mock()

    with patch.object(DirectoryRouteChain, "get_settings", return_value=route_settings):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/v1/transfer/route/settings")

    assert response.status_code == 200
    assert response.json()["data"]["match_mode"] == "specificity"
    assert response.json()["data"]["directories"][1]["name"] == "综艺"
