"""
豆瓣短评与影评端点的媒体类型路由测试。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.endpoints import douban as endpoint
from app.schemas.comment import MediaCommentPage, MediaReviewPage


@pytest.mark.asyncio
async def test_comment_endpoints_delegate_to_movie_and_tv_chains(monkeypatch) -> None:
    """
    短评和影评端点应按电影或电视剧类型调用对应异步链方法。
    """
    comment_page = MediaCommentPage(start=3, count=2, total=10)
    review_page = MediaReviewPage(start=3, count=2, total=10)
    chain = SimpleNamespace(
        async_movie_interests=AsyncMock(return_value=comment_page),
        async_tv_interests=AsyncMock(return_value=comment_page),
        async_movie_reviews=AsyncMock(return_value=review_page),
        async_tv_reviews=AsyncMock(return_value=review_page),
    )
    monkeypatch.setattr(endpoint, "DoubanChain", lambda: chain)

    assert await endpoint.douban_interests("1", "movie", start=3, count=2, _=None) == comment_page
    assert await endpoint.douban_interests("2", "tv", start=4, count=5, _=None) == comment_page
    assert await endpoint.douban_reviews("3", "movie", start=3, count=2, _=None) == review_page
    assert await endpoint.douban_reviews("4", "tv", start=4, count=5, _=None) == review_page

    chain.async_movie_interests.assert_awaited_once_with(doubanid="1", start=3, count=2)
    chain.async_tv_interests.assert_awaited_once_with(doubanid="2", start=4, count=5)
    chain.async_movie_reviews.assert_awaited_once_with(doubanid="3", start=3, count=2)
    chain.async_tv_reviews.assert_awaited_once_with(doubanid="4", start=4, count=5)


@pytest.mark.asyncio
async def test_comment_endpoints_return_empty_page_for_unknown_type() -> None:
    """
    未知媒体类型应返回对应的空分页结果。
    """
    assert await endpoint.douban_interests("1", "music", _=None) == MediaCommentPage()
    assert await endpoint.douban_reviews("1", "music", _=None) == MediaReviewPage()
