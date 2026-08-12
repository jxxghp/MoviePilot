import asyncio
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api.endpoints import media as media_endpoints
from app.api.endpoints.media import search
from app.chain import ChainBase
from app.core.security import verify_token
from app.modules.douban import DoubanModule
from app.modules.themoviedb import TheMovieDbModule
from app.schemas.types import MediaType


@pytest.mark.parametrize(
    ("search_type", "method_name", "source"),
    [
        ("collection", "async_search_collections", "themoviedb"),
        ("person", "async_search_persons", "douban"),
    ],
)
def test_media_search_endpoint_forwards_source(
    search_type: str, method_name: str, source: str
) -> None:
    """媒体搜索接口应将合集和人物的数据源下传到处理链。"""
    chain = Mock()
    search_method = AsyncMock(return_value=[])
    setattr(chain, method_name, search_method)

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        result = asyncio.run(
            search(
                title="测试",
                type=search_type,
                media_source=source,
                _=Mock(),
            )
        )

    assert result == []
    search_method.assert_awaited_once_with(name="测试", media_source=source)


def test_media_search_endpoint_forwards_multi_source() -> None:
    """媒体搜索接口应将逗号分隔的多数据源原样下传到处理链。"""
    chain = Mock()
    chain.async_search = AsyncMock(return_value=(Mock(), []))

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        result = asyncio.run(
            search(
                title="测试",
                type="media",
                media_source="themoviedb,douban",
                _=Mock(),
            )
        )

    assert result == []
    chain.async_search.assert_awaited_once_with(
        title="测试", media_source="themoviedb,douban"
    )


@pytest.mark.anyio
async def test_media_search_route_accepts_comma_separated_music_sources() -> None:
    """真实 FastAPI 路由应接受逗号分隔的数据源，而不能在参数校验阶段返回 422。"""
    chain = Mock()
    chain.async_search = AsyncMock(return_value=[])
    app = FastAPI()
    app.include_router(media_endpoints.router, prefix="/api/v1/media")
    app.dependency_overrides[verify_token] = lambda: Mock()

    with patch("app.api.endpoints.media.MusicChain", return_value=chain):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/v1/media/search",
                params={
                    "title": "周杰伦",
                    "type": "music",
                    "count": 30,
                    "media_source": "musicbrainz,theaudiodb,doubanmusic",
                },
            )

    assert response.status_code == 200
    assert response.json() == []
    chain.async_search.assert_awaited_once_with(
        query="周杰伦",
        limit=30,
        media_source="musicbrainz,theaudiodb,doubanmusic",
    )


@pytest.mark.parametrize(
    ("method_name", "module_method_name"),
    [
        ("async_search_persons", "async_search_persons"),
        ("async_search_collections", "async_search_collections"),
    ],
)
def test_chain_forwards_source_to_modules(
    method_name: str, module_method_name: str
) -> None:
    """处理链应将人物和合集的请求级数据源传递给媒体模块。"""
    chain = Mock(spec=ChainBase)
    chain.async_run_module = AsyncMock(return_value=[])

    result = asyncio.run(
        getattr(ChainBase, method_name)(
            chain,
            name="测试",
            media_source="themoviedb",
        )
    )

    assert result == []
    chain.async_run_module.assert_awaited_once_with(
        module_method_name,
        name="测试",
        media_source="themoviedb",
    )


def test_tmdb_person_search_respects_explicit_source(monkeypatch) -> None:
    """TMDB人物搜索应拒绝其他来源，并允许显式选择覆盖系统默认来源。"""
    monkeypatch.setattr("app.modules.themoviedb.settings.SEARCH_SOURCE", "douban")
    module = TheMovieDbModule()
    module.tmdb = Mock()
    module.tmdb.async_search_persons = AsyncMock(return_value=[])

    skipped = asyncio.run(
        module.async_search_persons(name="测试", media_source="douban")
    )
    result = asyncio.run(
        module.async_search_persons(name="测试", media_source="themoviedb")
    )

    assert skipped is None
    assert result == []
    module.tmdb.async_search_persons.assert_awaited_once_with("测试")


def test_douban_person_search_respects_explicit_source(monkeypatch) -> None:
    """豆瓣人物搜索应拒绝其他来源，并允许显式选择覆盖系统默认来源。"""
    monkeypatch.setattr(
        "app.modules.douban.settings.SEARCH_SOURCE", "themoviedb"
    )
    module = DoubanModule()
    module.doubanapi = Mock()
    module.doubanapi.async_person_search = AsyncMock(return_value={})

    skipped = asyncio.run(
        module.async_search_persons(name="测试", media_source="themoviedb")
    )
    result = asyncio.run(
        module.async_search_persons(name="测试", media_source="douban")
    )

    assert skipped is None
    assert result == []
    module.doubanapi.async_person_search.assert_awaited_once_with(keyword="测试")


def test_tmdb_collection_search_rejects_unsupported_source() -> None:
    """TMDB合集搜索不应处理非TMDB来源请求。"""
    module = TheMovieDbModule()
    module.tmdb = Mock()
    module.tmdb.async_search_collections = AsyncMock(return_value=[])

    result = asyncio.run(
        module.async_search_collections(name="测试", media_source="douban")
    )

    assert result is None
    module.tmdb.async_search_collections.assert_not_awaited()


def test_tmdb_collection_search_supports_multi_source_request() -> None:
    """TMDB合集搜索应支持请求级逗号分隔多数据源。"""
    module = TheMovieDbModule()
    module.tmdb = Mock()
    module.tmdb.async_search_collections = AsyncMock(return_value=[])

    result = asyncio.run(
        module.async_search_collections(
            name="测试", media_source="themoviedb,douban"
        )
    )

    assert result == []
    module.tmdb.async_search_collections.assert_awaited_once_with("测试")


def test_tmdb_media_search_supports_multi_source_request(monkeypatch) -> None:
    """TMDB媒体搜索应支持请求级逗号分隔多数据源，不被其他来源请求阻断。"""
    monkeypatch.setattr("app.modules.themoviedb.settings.SEARCH_SOURCE", "douban")
    module = TheMovieDbModule()
    module.tmdb = Mock()
    module.tmdb.search_multiis = Mock(return_value=[])
    meta = Mock()
    meta.name = "测试"
    meta.type = MediaType.UNKNOWN
    meta.year = None

    skipped = module.search_medias(meta=meta, media_source="douban")
    result = module.search_medias(meta=meta, media_source="themoviedb,douban")

    assert skipped is None
    assert result == []
    module.tmdb.search_multiis.assert_called_once_with("测试")


def test_douban_media_search_supports_multi_source_request(monkeypatch) -> None:
    """豆瓣媒体搜索应支持请求级逗号分隔多数据源，并跟随全局配置参与搜索。"""
    monkeypatch.setattr("app.modules.douban.settings.SEARCH_SOURCE", "themoviedb")
    module = DoubanModule()
    module.doubanapi = Mock()
    module.doubanapi.async_search = AsyncMock(return_value={"items": []})
    meta = Mock()
    meta.name = "测试"

    result = asyncio.run(
        module.async_search_medias(meta=meta, media_source="themoviedb,douban")
    )

    assert result == []
    module.doubanapi.async_search.assert_awaited_once_with("测试")


def test_multi_source_request_keeps_missing_module_skipped(monkeypatch) -> None:
    """请求级多数据源未包含的模块应跳过，避免无关模块参与搜索。"""
    monkeypatch.setattr("app.modules.douban.settings.SEARCH_SOURCE", "themoviedb")
    module = DoubanModule()
    module.doubanapi = Mock()
    module.doubanapi.async_search = AsyncMock(return_value={"items": []})
    meta = Mock()
    meta.name = "测试"

    skipped = asyncio.run(
        module.async_search_medias(meta=meta, media_source="themoviedb")
    )

    assert skipped is None
    module.doubanapi.async_search.assert_not_awaited()
