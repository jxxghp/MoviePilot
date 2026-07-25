from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api.endpoints.recommend import tmdb_movies, tmdb_trending, tmdb_tvs
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.modules.themoviedb.tmdbv3api.exceptions import TMDbException


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "chain_method"),
    [
        (tmdb_movies, "async_tmdb_movies"),
        (tmdb_tvs, "async_tmdb_tvs"),
        (tmdb_trending, "async_tmdb_trending"),
    ],
)
async def test_dashboard_recommend_endpoints_preserve_successful_empty_results(
    endpoint,
    chain_method,
):
    """TMDB 成功返回空列表时，推荐卡片接口应保留真实空结果。"""
    with patch("app.api.endpoints.recommend.RecommendChain") as chain_cls:
        chain_mock = AsyncMock(return_value=[])
        setattr(chain_cls.return_value, chain_method, chain_mock)

        result = await endpoint(
            page=1,
            _=SimpleNamespace(username="alice"),
        )

    assert result == []
    assert chain_mock.await_args.kwargs["raise_exception"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "chain_method"),
    [
        (tmdb_movies, "async_tmdb_movies"),
        (tmdb_tvs, "async_tmdb_tvs"),
        (tmdb_trending, "async_tmdb_trending"),
    ],
)
async def test_dashboard_recommend_endpoints_report_upstream_failures(
    endpoint,
    chain_method,
):
    """TMDB 请求异常时，推荐卡片接口应返回明确的网关错误。"""
    with patch("app.api.endpoints.recommend.RecommendChain") as chain_cls:
        setattr(
            chain_cls.return_value,
            chain_method,
            AsyncMock(side_effect=TMDbException("remote unavailable")),
        )

        with pytest.raises(HTTPException) as exc_info:
            await endpoint(
                page=1,
                _=SimpleNamespace(username="alice"),
            )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "TMDB请求失败"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "dependency_name", "dependency_method", "kwargs"),
    [
        (
            "async_discover_movies",
            "discover",
            "async_discover_movies",
            {"params": {"page": 1}},
        ),
        (
            "async_discover_tvs",
            "discover",
            "async_discover_tv_shows",
            {"params": {"page": 1}},
        ),
        (
            "async_discover_trending",
            "trending",
            "async_all_week",
            {"page": 1},
        ),
    ],
)
async def test_tmdb_recommend_queries_only_propagate_failures_in_strict_mode(
    method_name,
    dependency_name,
    dependency_method,
    kwargs,
):
    """推荐 endpoint 的严格模式应保留异常，其他调用方继续沿用空列表降级。"""
    api = TmdbApi.__new__(TmdbApi)
    dependency = SimpleNamespace(
        **{dependency_method: AsyncMock(side_effect=TMDbException("remote unavailable"))}
    )
    setattr(api, dependency_name, dependency)

    assert await getattr(api, method_name)(**kwargs) == []

    with pytest.raises(TMDbException, match="remote unavailable"):
        await getattr(api, method_name)(**kwargs, raise_exception=True)
