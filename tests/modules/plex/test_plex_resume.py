"""
继续观看直接使用列表字段，缺失的可选信息不能触发逐条详情请求。
"""

from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
from plexapi.server import PlexServer

from app.modules.plex.plex import Plex


@pytest.mark.parametrize(
    ("attributes", "expected_subtitle", "expected_image", "expected_percent"),
    [
        ('type="movie" viewOffset="1000"', None, None, 0),
        ('type="movie" year="2026" duration="10000" art="/art.jpg"', "2026", "/art.jpg", 0),
        ('type="movie" year="2026" viewOffset="1000" art="/art.jpg"', "2026", "/art.jpg", 0),
        ('type="movie" year="2026" art="/art.jpg" viewOffset="4000" duration="10000"',
         "2026", "/art.jpg", 40),
        ('type="episode" grandparentTitle="剧集" parentIndex="1" index="2" '
         'grandparentArt="/show-art.jpg" viewOffset="1000"',
         "S1:E2 - 测试条目", "/show-art.jpg", 0),
        ('type="episode" grandparentTitle="剧集" parentIndex="1" index="2" '
         'duration="10000" viewOffset="2500"', "S1:E2 - 测试条目", None, 25),
    ],
    ids=["missing-year-duration-art", "missing-offset", "missing-duration", "complete-movie",
         "episode-fallback-art-missing-duration", "episode-missing-art"],
)
def test_resume_uses_hub_fields_without_detail_requests(
    attributes: str, expected_subtitle: Optional[str], expected_image: Optional[str], expected_percent: int,
) -> None:
    """
    使用真实 PlexAPI 解析列表，禁止缺字段时请求详情，同时保留图片回退与观看进度。
    """
    session = Mock()

    def respond(url: str, **_kwargs: object) -> SimpleNamespace:
        path = urlsplit(url).path
        if path == "/":
            text = '<MediaContainer machineIdentifier="test-server"/>'
        elif path == "/hubs/continueWatching/items":
            text = (
                '<MediaContainer size="1" totalSize="1">'
                f'<Video key="/library/metadata/1" ratingKey="1" title="测试条目" {attributes}/>'
                '</MediaContainer>'
            )
        else:
            raise AssertionError(f"列表展示不应额外请求 {path}")
        return SimpleNamespace(status_code=200, text=text)

    session.get.side_effect = respond
    session.get.__name__ = "get"
    server = PlexServer("http://plex.test:32400", "test-token", session=session)
    session.get.reset_mock()
    client = Plex.__new__(Plex)
    client._plex = server
    client._host = "http://plex.test:32400/"
    client._playhost = None
    client._token = "test-token"
    client.get_librarys = Mock(return_value=[SimpleNamespace(id="2")])

    result = client.get_resume(8)

    assert len(result) == 1
    assert result[0].id == "/library/metadata/1"
    assert result[0].subtitle == expected_subtitle
    assert result[0].percent == expected_percent
    assert result[0].image == (
        f"http://plex.test:32400{expected_image}?X-Plex-Token=test-token" if expected_image else None
    )
    assert "/server/test-server/details?key=/library/metadata/1" in result[0].link
    client.get_librarys.assert_called_once_with(hidden=True)
    session.get.assert_called_once()
    call = session.get.call_args
    assert urlsplit(call.args[0]).path == "/hubs/continueWatching/items"
    assert call.kwargs["params"] == {"contentDirectoryID": "2"}
    assert call.kwargs["headers"]["X-Plex-Container-Size"] == "8"
