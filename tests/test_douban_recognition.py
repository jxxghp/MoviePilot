import asyncio
from unittest.mock import Mock
from unittest.mock import AsyncMock

from app.core.meta import MetaBase
from app.modules.douban import DoubanModule
from app.schemas.types import MediaType


def test_douban_recognize_does_not_keep_dedicated_mapping_cache():
    """豆瓣识别应每次执行匹配，不再保留专用标题映射缓存。"""
    module = DoubanModule()
    meta = MetaBase("测试电影")
    meta.name = "测试电影"
    meta.type = MediaType.MOVIE
    meta.year = "2024"
    match_doubaninfo = Mock(return_value={"id": "200"})
    douban_info = Mock(return_value={
        "id": "200",
        "title": "测试电影",
        "type": "movie",
        "year": "2024",
    })

    first_result = module._recognize_media_core(
        meta=meta,
        source="douban",
        match_doubaninfo_func=match_doubaninfo,
        douban_info_func=douban_info,
    )
    second_result = module._recognize_media_core(
        meta=meta,
        source="douban",
        match_doubaninfo_func=match_doubaninfo,
        douban_info_func=douban_info,
    )

    assert first_result.douban_id == "200"
    assert second_result.douban_id == "200"
    assert match_doubaninfo.call_count == 2
    assert douban_info.call_count == 2


def test_async_douban_recognize_does_not_keep_dedicated_mapping_cache():
    """异步豆瓣识别也应每次执行匹配，不使用专用标题映射缓存。"""
    module = DoubanModule()
    meta = MetaBase("测试剧集")
    meta.name = "测试剧集"
    meta.type = MediaType.TV
    meta.year = "2024"
    match_doubaninfo = AsyncMock(return_value={"id": "201"})
    douban_info = AsyncMock(return_value={
        "id": "201",
        "title": "测试剧集",
        "type": "tv",
        "year": "2024",
    })

    async def recognize_twice():
        """连续执行两次异步豆瓣识别。"""
        first_result = await module._async_recognize_media_core(
            meta=meta,
            source="douban",
            async_match_doubaninfo_func=match_doubaninfo,
            async_douban_info_func=douban_info,
        )
        second_result = await module._async_recognize_media_core(
            meta=meta,
            source="douban",
            async_match_doubaninfo_func=match_doubaninfo,
            async_douban_info_func=douban_info,
        )
        return first_result, second_result

    first_result, second_result = asyncio.run(recognize_twice())

    assert first_result.douban_id == "201"
    assert second_result.douban_id == "201"
    assert match_doubaninfo.await_count == 2
    assert douban_info.await_count == 2
