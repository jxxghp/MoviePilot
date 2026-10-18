"""
豆瓣短评与影评投影的同步异步一致性测试。
"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.douban import DoubanModule
from app.runtime.extensions.module.contracts import diagnose_module_result


def _module() -> DoubanModule:
    """
    构造只注入离线评论客户端的豆瓣模块。
    """
    module = object.__new__(DoubanModule)
    module.doubanapi = Mock()
    for name in (
        "movie_interests",
        "tv_interests",
        "movie_reviews",
        "tv_reviews",
        "async_movie_interests",
        "async_tv_interests",
        "async_movie_reviews",
        "async_tv_reviews",
    ):
        setattr(module.doubanapi, name, AsyncMock() if name.startswith("async_") else Mock())
    return module


def _interests_response() -> dict[str, object]:
    """
    构造包含完整字段和缺失可选字段的短评响应。
    """
    return {
        "start": 0,
        "count": 2,
        "total": 101153,
        "interests": [
            {
                "id": 3055283274,
                "comment": "还行  可是古巴的007女郎比女主好看啊",
                "rating": {"count": 1, "max": 5, "star_count": 4, "value": 4},
                "vote_count": 7652,
                "create_time": "2021-10-29 12:00:00",
                "sharing_url": "https://movie.douban.com/interest/1/",
                "user": {
                    "id": "221011676",
                    "name": "陀螺凡达可",
                    "avatar": "https://img2.doubanio.com/icon/up221011676-2.jpg",
                    "url": "https://www.douban.com/people/221011676/",
                },
            },
            {"id": 2, "comment": "未评分短评", "rating": {"value": 0}},
        ],
    }


def _reviews_response() -> dict[str, object]:
    """
    构造包含完整字段和缺失可选字段的影评响应。
    """
    return {
        "start": 0,
        "count": 1,
        "total": 956,
        "reviews": [
            {
                "id": 13923501,
                "title": "《无暇赴死》或许很好，假如它拍的不是詹姆斯·邦德",
                "abstract": "以下严重剧透注意。",
                "rating": {"value": 3},
                "useful_count": 2166,
                "comments_count": 42,
                "create_time": "2021-10-29 12:00:00",
                "url": "https://movie.douban.com/review/13923501/",
                "user": {"id": "1", "name": "Argenore"},
            },
            {"id": 2, "title": "无正文影评"},
        ],
    }


def test_interests_projection_normalizes_fields_and_author() -> None:
    """
    短评投影应归一评分为浮点、补齐作者并保留分页元数据。
    """
    module = _module()

    page = module._process_interests(_interests_response())

    assert (page.start, page.count, page.total) == (0, 2, 101153)
    first, second = page.items
    assert first.id == "3055283274"
    assert first.rating == 4.0
    assert first.vote_count == 7652
    assert first.author is not None
    assert first.author.name == "陀螺凡达可"
    assert first.author.url == "https://www.douban.com/people/221011676/"
    assert first.url == "https://movie.douban.com/interest/1/"
    # 未评分与缺失作者必须投影为 None，而不是 0 或空对象
    assert second.rating is None
    assert second.author is None


def test_reviews_projection_normalizes_fields_and_author() -> None:
    """
    影评投影应保留标题摘要、有用数并补齐作者。
    """
    module = _module()

    page = module._process_reviews(_reviews_response())

    assert (page.start, page.count, page.total) == (0, 1, 956)
    first, second = page.items
    assert first.id == "13923501"
    assert first.title == "《无暇赴死》或许很好，假如它拍的不是詹姆斯·邦德"
    assert first.abstract == "以下严重剧透注意。"
    assert first.rating == 3.0
    assert first.useful_count == 2166
    assert first.comments_count == 42
    assert first.author is not None
    assert first.author.name == "Argenore"
    assert second.rating is None
    assert second.abstract is None


@pytest.mark.parametrize("payload", [None, {}])
def test_empty_response_yields_empty_page(payload: dict[str, object] | None) -> None:
    """
    空响应或空字典必须返回空分页结果而不是抛错。
    """
    module = _module()

    interests = module._process_interests(payload)
    reviews = module._process_reviews(payload)

    assert (interests.start, interests.count, interests.total, interests.items) == (0, 0, 0, [])
    assert (reviews.start, reviews.count, reviews.total, reviews.items) == (0, 0, 0, [])


def test_projection_does_not_mutate_source_payload() -> None:
    """
    投影必须保持源响应不变，避免污染豆瓣客户端缓存。
    """
    module = _module()
    source = _interests_response()
    original = deepcopy(source)

    module._process_interests(source)

    assert source == original


@pytest.mark.parametrize(
    ("kind", "sync_method", "async_method"),
    [
        ("interests", "douban_movie_interests", "async_douban_movie_interests"),
        ("interests", "douban_tv_interests", "async_douban_tv_interests"),
        ("reviews", "douban_movie_reviews", "async_douban_movie_reviews"),
        ("reviews", "douban_tv_reviews", "async_douban_tv_reviews"),
    ],
)
def test_sync_and_async_entries_share_projection(
    kind: str, sync_method: str, async_method: str
) -> None:
    """
    同步与异步入口应传递相同分页参数并产出相同结果。
    """
    module = _module()
    response = _interests_response() if kind == "interests" else _reviews_response()
    client_method = sync_method.removeprefix("douban_")
    getattr(module.doubanapi, client_method).return_value = deepcopy(response)
    getattr(module.doubanapi, f"async_{client_method}").return_value = deepcopy(response)

    sync_result = getattr(module, sync_method)("100", start=5, count=10)
    async_result = asyncio.run(getattr(module, async_method)("100", start=5, count=10))

    assert sync_result == async_result
    assert diagnose_module_result(sync_method, sync_result) == ()
    assert diagnose_module_result(async_method, async_result) == ()
    assert diagnose_module_result(sync_method, {}) == ("unexpected-result:model:dict",)
    assert diagnose_module_result(async_method, []) == ("unexpected-result:model:list",)
    getattr(module.doubanapi, client_method).assert_called_once_with(
        subject_id="100", start=5, count=10
    )
    getattr(module.doubanapi, f"async_{client_method}").assert_awaited_once_with(
        subject_id="100", start=5, count=10
    )
