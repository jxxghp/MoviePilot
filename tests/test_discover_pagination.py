from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.endpoints.douban import douban_recommend
from app.api.endpoints.tmdb import tmdb_recommend, tmdb_similar


def _media(index: int) -> SimpleNamespace:
    """构造只满足发现端点序列化边界的媒体替身。"""
    return SimpleNamespace(to_dict=lambda: {"title": f"媒体 {index}"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "chain_name", "method_name", "identifier", "type_name"),
    [
        (tmdb_similar, "TmdbChain", "async_movie_similar", 100, "电影"),
        (tmdb_recommend, "TmdbChain", "async_movie_recommend", 100, "电影"),
    ],
)
async def test_tmdb_browse_recommendations_apply_page_and_count(
    endpoint, chain_name, method_name, identifier, type_name
):
    """TMDB browse 推荐和类似列表必须返回请求页，不重复发送整组结果。"""
    medias = [_media(index) for index in range(1, 6)]
    with patch(f"app.api.endpoints.tmdb.{chain_name}") as chain_cls:
        chain_method = AsyncMock(return_value=medias)
        setattr(chain_cls.return_value, method_name, chain_method)

        result = await endpoint(
            tmdbid=identifier,
            type_name=type_name,
            page=2,
            count=2,
            _=None,
        )

    assert result == [{"title": "媒体 3"}, {"title": "媒体 4"}]
    chain_method.assert_awaited_once_with(tmdbid=identifier)


@pytest.mark.asyncio
async def test_douban_browse_recommendations_return_empty_terminal_page():
    """豆瓣推荐超出结果范围时返回空页，供前端无限列表结束加载。"""
    medias = [_media(index) for index in range(1, 4)]
    with patch("app.api.endpoints.douban.DoubanChain") as chain_cls:
        chain_method = AsyncMock(return_value=medias)
        chain_cls.return_value.async_movie_recommend = chain_method

        result = await douban_recommend(
            doubanid="db-1",
            type_name="电影",
            page=2,
            count=3,
            _=None,
        )

    assert result == []
    chain_method.assert_awaited_once_with(doubanid="db-1")
