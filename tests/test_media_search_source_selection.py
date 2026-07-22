import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api.endpoints.media import search
from app.chain import ChainBase
from app.modules.douban import DoubanModule
from app.modules.themoviedb import TheMovieDbModule


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
                source=source,
                _=Mock(),
            )
        )

    assert result == []
    search_method.assert_awaited_once_with(name="测试", source=source)


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
            source="themoviedb",
        )
    )

    assert result == []
    chain.async_run_module.assert_awaited_once_with(
        module_method_name,
        name="测试",
        source="themoviedb",
    )


def test_tmdb_person_search_respects_explicit_source(monkeypatch) -> None:
    """TMDB人物搜索应拒绝其他来源，并允许显式选择覆盖系统默认来源。"""
    monkeypatch.setattr("app.modules.themoviedb.settings.SEARCH_SOURCE", "douban")
    module = TheMovieDbModule()
    module.tmdb = Mock()
    module.tmdb.async_search_persons = AsyncMock(return_value=[])

    skipped = asyncio.run(
        module.async_search_persons(name="测试", source="douban")
    )
    result = asyncio.run(
        module.async_search_persons(name="测试", source="themoviedb")
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
        module.async_search_persons(name="测试", source="themoviedb")
    )
    result = asyncio.run(
        module.async_search_persons(name="测试", source="douban")
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
        module.async_search_collections(name="测试", source="douban")
    )

    assert result is None
    module.tmdb.async_search_collections.assert_not_awaited()
