from unittest.mock import Mock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api.endpoints import transfer as transfer_endpoint
from app.chain.transfer import TransferChain
from app.db.user_oper import get_current_active_manage_user
from app.schemas import (
    CategoryConfig,
    CategoryRule,
    TransferDirectoryConf,
    TransferRouteMediaSnapshot,
    TransferRoutePreviewRequest,
)
from app.schemas.types import DirectoryMatchMode, MediaType


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


def test_transfer_chain_preview_compares_modes_without_network() -> None:
    """路由预览应复用同一快照比较两种模式且不触发外部识别。"""
    response = TransferChain.preview_route(_preview_request())

    assert response.category.selected_category == "综艺"
    assert response.metadata["genre_ids"] == [10764]
    assert response.category.source == "automatic"
    assert response.route.mode == DirectoryMatchMode.SPECIFICITY
    assert response.route.selected_directory.name == "综艺"
    decisions = {decision.mode: decision for decision in response.comparisons}
    assert decisions[DirectoryMatchMode.SEQUENTIAL].selected_directory.name == "通用电视剧"
    assert decisions[DirectoryMatchMode.SPECIFICITY].selected_directory.name == "综艺"


def test_transfer_chain_preview_preserves_provided_category_and_warns_on_conflict() -> None:
    """订阅或历史提供的类别优先于自动分类，并明确报告冲突。"""
    response = TransferChain.preview_route(_preview_request(category="纪录片"))

    assert response.category.automatic_category == "综艺"
    assert response.category.selected_category == "纪录片"
    assert response.category.source == "provided"
    assert any(warning.code == "provided_category_conflict" for warning in response.category.warnings)


def test_transfer_chain_preview_reads_current_configs_when_drafts_are_omitted(monkeypatch) -> None:
    """未传草稿时只读取本地配置，不调用媒体识别或 TMDB。"""
    request = _preview_request()
    request.category_config = None
    request.directories = None
    category_config = CategoryConfig(tv={"综艺": CategoryRule(genre_ids="10764")})
    directories = _preview_request().directories
    monkeypatch.setattr(
        "app.chain.transfer.CategoryHelper.load",
        lambda _self: category_config,
    )
    monkeypatch.setattr(
        "app.chain.transfer.DirectoryHelper.get_dirs",
        lambda _self: directories,
    )

    with patch("app.chain.transfer.MediaChain") as media_chain:
        response = TransferChain.preview_route(request)

    media_chain.assert_not_called()
    assert response.route.selected_directory.name == "综艺"


@pytest.mark.anyio
async def test_route_preview_endpoint_uses_typed_chain_contract() -> None:
    """REST 入口应校验请求并把业务编排交给 TransferChain。"""
    request = _preview_request()
    expected = TransferChain.preview_route(request)
    app = FastAPI()
    app.include_router(transfer_endpoint.router, prefix="/api/v1/transfer")
    app.dependency_overrides[get_current_active_manage_user] = lambda: Mock()

    with patch.object(TransferChain, "preview_route", return_value=expected) as preview:
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
