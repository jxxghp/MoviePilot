import asyncio
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api.endpoints import media as media_endpoints
from app.api.endpoints.media import search
from app.application.orchestration import ChainBase
from app.adapters.web.security.access import verify_token
from app.modules.douban import DoubanModule
from app.modules.themoviedb import TheMovieDbModule
from app.schemas.types import MediaSource, MediaType


@pytest.mark.parametrize(
    ("search_type", "method_name", "source"),
    [
        ("collection", "async_search_collections", MediaSource.TMDB),
        ("person", "async_search_persons", MediaSource.Douban),
    ],
)
def test_media_search_endpoint_forwards_source(
    search_type: str, method_name: str, source: MediaSource
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
                media_source=source.value,
                _=Mock(),
            )
        )

    assert result == []
    search_method.assert_awaited_once_with(
        name="测试", media_source=(source,)
    )


def test_media_search_endpoint_forwards_multi_source() -> None:
    """媒体搜索接口应将逗号分隔来源解析为规范来源元组。"""
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
        title="测试",
        media_source=(MediaSource.TMDB, MediaSource.Douban),
    )


@pytest.mark.anyio
async def test_media_search_route_accepts_comma_separated_music_sources() -> None:
    """真实路由在旧逗号格式兼容边界后应把每项转换为 MediaSource。"""
    chain = Mock()
    chain.async_search_music = AsyncMock(return_value=[])
    app = FastAPI()
    app.include_router(media_endpoints.router, prefix="/api/v1/media")
    app.dependency_overrides[verify_token] = lambda: Mock()

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
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
    assert response.json() == {"success": True, "message": "", "data": []}
    chain.async_search_music.assert_awaited_once_with(
        query="周杰伦",
        limit=30,
        media_source=(
            MediaSource.MusicBrainz,
            MediaSource.TheAudioDB,
            MediaSource.DoubanMusic,
        ),
    )


@pytest.mark.anyio
async def test_media_search_route_accepts_repeated_enum_sources() -> None:
    """新客户端可用重复查询参数传入多个媒体来源枚举。"""
    chain = Mock()
    chain.async_search = AsyncMock(return_value=(Mock(), []))
    app = FastAPI()
    app.include_router(media_endpoints.router, prefix="/api/v1/media")
    app.dependency_overrides[verify_token] = lambda: Mock()

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/v1/media/search",
                params=[
                    ("title", "测试"),
                    ("media_source", MediaSource.TMDB.value),
                    ("media_source", MediaSource.Douban.value),
                ],
            )

    assert response.status_code == 200
    chain.async_search.assert_awaited_once_with(
        title="测试",
        media_source=(MediaSource.TMDB, MediaSource.Douban),
    )


@pytest.mark.anyio
async def test_media_search_route_deduplicates_repeated_sources() -> None:
    """重复来源参数应按首次出现顺序去重，避免同一模块重复搜索。"""
    chain = Mock()
    chain.async_search = AsyncMock(return_value=(Mock(), []))
    app = FastAPI()
    app.include_router(media_endpoints.router, prefix="/api/v1/media")
    app.dependency_overrides[verify_token] = lambda: Mock()

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/v1/media/search",
                params=[
                    ("title", "测试"),
                    ("media_source", MediaSource.Douban.value),
                    ("media_source", MediaSource.TMDB.value),
                    ("media_source", MediaSource.Douban.value),
                ],
            )

    assert response.status_code == 200
    chain.async_search.assert_awaited_once_with(
        title="测试",
        media_source=(MediaSource.Douban, MediaSource.TMDB),
    )


@pytest.mark.anyio
async def test_media_search_route_forwards_plugin_source() -> None:
    """真实搜索路由应把插件扩展来源传入完整模块调度。"""
    chain = Mock()
    chain.async_search = AsyncMock(return_value=(Mock(), []))
    app = FastAPI()
    app.include_router(media_endpoints.router, prefix="/api/v1/media")
    app.dependency_overrides[verify_token] = lambda: Mock()

    with patch("app.api.endpoints.media.MediaChain", return_value=chain):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/v1/media/search",
                params={"title": "测试", "media_source": "plugin-source"},
            )

    assert response.status_code == 200
    chain.async_search.assert_awaited_once_with(
        title="测试",
        media_source=(MediaSource("plugin-source"),),
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
    chain.async_multicast = AsyncMock(return_value=[])

    result = asyncio.run(
        getattr(ChainBase, method_name)(
            chain,
            name="测试",
            media_source=MediaSource.TMDB,
        )
    )

    assert result == []
    chain.async_multicast.assert_awaited_once_with(
        module_method_name,
        name="测试",
        media_source=MediaSource.TMDB,
    )


def test_tmdb_person_search_respects_explicit_source(monkeypatch) -> None:
    """TMDB人物搜索应拒绝其他来源，并允许显式选择覆盖系统默认来源。"""
    monkeypatch.setattr("app.modules.themoviedb.settings.SEARCH_SOURCE", "douban")
    module = TheMovieDbModule()
    module.tmdb = Mock()
    module.tmdb.async_search_persons = AsyncMock(return_value=[])

    skipped = asyncio.run(
        module.async_search_persons(name="测试", media_source=MediaSource.Douban)
    )
    result = asyncio.run(
        module.async_search_persons(name="测试", media_source=MediaSource.TMDB)
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
        module.async_search_persons(name="测试", media_source=MediaSource.TMDB)
    )
    result = asyncio.run(
        module.async_search_persons(name="测试", media_source=MediaSource.Douban)
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
        module.async_search_collections(name="测试", media_source=MediaSource.Douban)
    )

    assert result is None
    module.tmdb.async_search_collections.assert_not_awaited()


def test_tmdb_collection_search_supports_multi_source_request() -> None:
    """TMDB合集搜索应支持请求级多数据源枚举元组。"""
    module = TheMovieDbModule()
    module.tmdb = Mock()
    module.tmdb.async_search_collections = AsyncMock(return_value=[])

    result = asyncio.run(
        module.async_search_collections(
            name="测试",
            media_source=(MediaSource.TMDB, MediaSource.Douban),
        )
    )

    assert result == []
    module.tmdb.async_search_collections.assert_awaited_once_with("测试")


def test_tmdb_media_search_supports_multi_source_request(monkeypatch) -> None:
    """TMDB媒体搜索应支持请求级多数据源枚举元组。"""
    monkeypatch.setattr("app.modules.themoviedb.settings.SEARCH_SOURCE", "douban")
    module = TheMovieDbModule()
    module.tmdb = Mock()
    module.tmdb.search_multiis = Mock(return_value=[])
    meta = Mock()
    meta.name = "测试"
    meta.type = MediaType.UNKNOWN
    meta.year = None

    skipped = module.search_medias(meta=meta, media_source=MediaSource.Douban)
    result = module.search_medias(
        meta=meta,
        media_source=(MediaSource.TMDB, MediaSource.Douban),
    )

    assert skipped is None
    assert result == []
    module.tmdb.search_multiis.assert_called_once_with("测试")


def test_douban_media_search_supports_multi_source_request(monkeypatch) -> None:
    """豆瓣媒体搜索应支持请求级多数据源枚举元组。"""
    monkeypatch.setattr("app.modules.douban.settings.SEARCH_SOURCE", "themoviedb")
    module = DoubanModule()
    module.doubanapi = Mock()
    module.doubanapi.async_search = AsyncMock(return_value={"items": []})
    meta = Mock()
    meta.name = "测试"

    result = asyncio.run(
        module.async_search_medias(
            meta=meta,
            media_source=(MediaSource.TMDB, MediaSource.Douban),
        )
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
        module.async_search_medias(meta=meta, media_source=MediaSource.TMDB)
    )

    assert skipped is None
    module.doubanapi.async_search.assert_not_awaited()
