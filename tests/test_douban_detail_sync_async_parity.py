"""豆瓣影视详情同步与异步入口的业务决策一致性测试。"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.douban import DoubanModule
from app.schemas.types import MediaType


def _module() -> DoubanModule:
    """构造只注入离线详情客户端的豆瓣模块。"""
    module = object.__new__(DoubanModule)
    module.doubanapi = Mock()
    module.doubanapi.async_movie_detail = AsyncMock()
    module.doubanapi.async_tv_detail = AsyncMock()
    module.doubanapi.async_movie_celebrities = AsyncMock()
    module.doubanapi.async_tv_celebrities = AsyncMock()
    return module


def _detail(media_type: str, title: str) -> dict[str, object]:
    """构造可验证类型回退和人物合并的最小豆瓣详情。"""
    return {"id": "100", "type": media_type, "title": title}


def test_unknown_type_sync_async_share_movie_first_tv_fallback() -> None:
    """未知类型时双入口都应先查电影，未命中后再查电视剧并合并人物。"""
    module = _module()
    sync_detail = _detail("tv", "测试剧")
    async_detail = deepcopy(sync_detail)
    celebrities = {
        "directors": [{"id": "1", "name": "导演"}],
        "actors": [{"id": "2", "name": "演员"}],
    }
    module.doubanapi.movie_detail.return_value = None
    module.doubanapi.tv_detail.return_value = sync_detail
    module.doubanapi.tv_celebrities.return_value = celebrities
    module.doubanapi.async_movie_detail.return_value = None
    module.doubanapi.async_tv_detail.return_value = async_detail
    module.doubanapi.async_tv_celebrities.return_value = deepcopy(celebrities)

    sync_result = module.douban_info("100")
    async_result = asyncio.run(module.async_douban_info("100"))

    assert sync_result == async_result == {**_detail("tv", "测试剧"), **celebrities}
    assert sync_result is sync_detail
    assert async_result is async_detail
    module.doubanapi.movie_detail.assert_called_once_with("100")
    module.doubanapi.tv_detail.assert_called_once_with("100")
    module.doubanapi.movie_celebrities.assert_not_called()
    module.doubanapi.tv_celebrities.assert_called_once_with("100")
    module.doubanapi.async_movie_detail.assert_awaited_once_with("100")
    module.doubanapi.async_tv_detail.assert_awaited_once_with("100")
    module.doubanapi.async_movie_celebrities.assert_not_awaited()
    module.doubanapi.async_tv_celebrities.assert_awaited_once_with("100")


@pytest.mark.parametrize(
    ("media_type", "detail_method", "celebrity_method"),
    [
        (MediaType.MOVIE, "movie_detail", "movie_celebrities"),
        (MediaType.TV, "tv_detail", "tv_celebrities"),
    ],
)
def test_explicit_type_sync_async_use_same_detail_branch(
    media_type: MediaType, detail_method: str, celebrity_method: str
) -> None:
    """显式类型时双入口只访问对应详情分支并采用相同人物投影。"""
    module = _module()
    source = _detail(media_type.value, "指定类型")
    celebrities = {"directors": [], "actors": [{"id": "2"}]}
    getattr(module.doubanapi, detail_method).return_value = deepcopy(source)
    getattr(module.doubanapi, celebrity_method).return_value = celebrities
    getattr(module.doubanapi, f"async_{detail_method}").return_value = deepcopy(source)
    getattr(
        module.doubanapi, f"async_{celebrity_method}"
    ).return_value = deepcopy(celebrities)

    sync_result = module.douban_info("100", media_type)
    async_result = asyncio.run(module.async_douban_info("100", media_type))

    assert sync_result == async_result == {**source, **celebrities}
    getattr(module.doubanapi, detail_method).assert_called_once_with("100")
    getattr(module.doubanapi, celebrity_method).assert_called_once_with("100")
    getattr(
        module.doubanapi, f"async_{detail_method}"
    ).assert_awaited_once_with("100")
    getattr(
        module.doubanapi, f"async_{celebrity_method}"
    ).assert_awaited_once_with("100")
    other_type = "tv" if media_type == MediaType.MOVIE else "movie"
    getattr(module.doubanapi, f"{other_type}_detail").assert_not_called()
    getattr(module.doubanapi, f"{other_type}_celebrities").assert_not_called()
    getattr(
        module.doubanapi, f"async_{other_type}_detail"
    ).assert_not_awaited()
    getattr(
        module.doubanapi, f"async_{other_type}_celebrities"
    ).assert_not_awaited()


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (None, "missing"),
        ({}, "missing"),
        ({"msg": "subject_ip_rate_limit"}, "rate_limited"),
        ({"id": "100"}, "matched"),
    ],
)
def test_detail_response_classification_is_shared_and_pure(
    info: dict[str, object] | None, expected: str
) -> None:
    """详情响应分类不应依赖同步或异步客户端状态。"""
    original = deepcopy(info)

    assert DoubanModule._classify_douban_detail(info) == expected
    assert info == original
