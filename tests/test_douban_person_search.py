"""
豆瓣人物搜索图片投影的离线回归测试。
"""

import asyncio
from copy import deepcopy
from typing import Optional
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.douban import DoubanModule
from app.schemas.types import MediaSource


@pytest.mark.parametrize(
    ("cover", "expected"),
    [
        (
            {"large": {"url": "https://example.com/large.webp", "width": 0, "height": 0},
             "normal": {"url": "https://example.com/normal.jpg", "width": 0, "height": 0}},
            {"large": "https://example.com/large.webp", "normal": "https://example.com/normal.jpg"},
        ),
        ({"large": "https://example.com/large.jpg"}, {"large": "https://example.com/large.jpg"}),
        (
            {"large": {"url": "https://example.com/large.jpg"}, "normal": "https://example.com/normal.jpg"},
            {"large": "https://example.com/large.jpg", "normal": "https://example.com/normal.jpg"},
        ),
        ({"large": {}, "normal": {"url": None}, "small": None}, {}),
        ({}, {}),
        (None, {}),
    ],
)
def test_person_search_normalizes_images_in_sync_and_async_entries(
    cover: Optional[dict[str, object]], expected: dict[str, str],
) -> None:
    """
    两个入口均应保留匹配人物及头像，并兼容嵌套、字符串和空图片。

    :param cover: 豆瓣返回的尺寸图片数据
    :param expected: 标准人物模型中应保留的有效尺寸 URL
    """
    response = {"items": [
        {"target_id": "100", "target": {
            "title": "赵露思", "url": "https://example.com/person/100",
            "cover": cover, "cover_img": {"url": "https://example.com/l/public/avatar.jpg"},
        }},
        {"target_id": "101", "target": {"title": "赵露思（演员）"}},
        {"target_id": "102", "target": {"title": "其他人物"}},
    ]}
    original = deepcopy(response)
    module = object.__new__(DoubanModule)
    module.doubanapi = Mock()
    module.doubanapi.person_search.return_value = response
    module.doubanapi.async_person_search = AsyncMock(return_value=response)

    sync_result = module.search_persons("赵露思", media_source=MediaSource.Douban)
    async_result = asyncio.run(module.async_search_persons("赵露思", media_source=MediaSource.Douban))

    assert sync_result == async_result
    assert [person.id for person in sync_result] == ["100", "101"]
    assert sync_result[0].source == "douban"
    assert sync_result[0].name == "赵露思"
    assert sync_result[0].url == "https://example.com/person/100"
    assert sync_result[0].avatar == "https://example.com/s/public/avatar.jpg"
    assert sync_result[0].images.model_dump(exclude_none=True) == expected
    assert sync_result[1].images.model_dump(exclude_none=True) == {}
    assert response == original
    module.doubanapi.person_search.assert_called_once_with(keyword="赵露思")
    module.doubanapi.async_person_search.assert_awaited_once_with(keyword="赵露思")
