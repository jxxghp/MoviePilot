"""
飞牛媒体服务器类型迁移后的边界分支测试。

测试只使用本地替身，不连接真实飞牛服务。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.trimemedia import api as trimemedia_api
from app.modules.trimemedia.module import TrimeMediaModule
from app.modules.trimemedia.trimemedia import TrimeMedia
from app.schemas.types import MediaSource


def _authenticated_client() -> TrimeMedia:
    """
    构造一个带有最小认证状态的飞牛客户端替身。
    """
    client = TrimeMedia.__new__(TrimeMedia)
    client._host = "https://trimemedia.example"
    client._username = "admin"
    client._password = "password"
    client._access_code = "access"
    client._playhost = None
    client._sync_libraries = []
    client._userinfo = trimemedia_api.User("user", "admin", 1)
    client._api = SimpleNamespace(
        host="https://trimemedia.example",
        token="token",
        cookies={"Trim-MC-access": "access"},
    )
    client.is_authenticated = Mock(return_value=True)
    return client


def test_authenticate_returns_none_when_probe_fails(monkeypatch):
    """
    临时认证 API 探测失败时返回 None。
    """
    client = TrimeMedia.__new__(TrimeMedia)
    client._host = "https://trimemedia.example"
    client._username = "admin"
    client._password = "password"
    monkeypatch.setattr(
        TrimeMedia,
        "_TrimeMedia__create_api",
        staticmethod(lambda *_args: None),
    )

    assert client.authenticate("admin", "password") is None


def test_get_items_yields_nested_directory_items():
    """
    媒体库目录应递归展开，并只产出电影条目。
    """
    client = _authenticated_client()
    root = trimemedia_api.Item(guid="root", type=trimemedia_api.Type.DIRECTORY)
    movie = trimemedia_api.Item(guid="movie", type=trimemedia_api.Type.MOVIE)
    client._api.item_list = Mock(side_effect=[[root], [movie]])
    client._TrimeMedia__build_media_server_item = Mock(return_value="movie-result")

    assert list(client.get_items("library")) == ["movie-result"]
    assert client._api.item_list.call_args_list[1].kwargs["guid"] == "root"


def test_get_tv_episodes_skips_incomplete_episode_numbers():
    """
    飞牛返回缺少季号或集号的条目时，只保留可用于统一契约的完整编号。
    """
    client = _authenticated_client()
    client.get_iteminfo = Mock(
        return_value=SimpleNamespace(media_source=MediaSource.TMDB, media_id="1")
    )
    client._api.season_list = Mock(
        return_value=[SimpleNamespace(guid="season", season_number=1)]
    )
    client._api.episode_list = Mock(
        return_value=[
            SimpleNamespace(season_number=None, episode_number=1),
            SimpleNamespace(season_number=1, episode_number=None),
            SimpleNamespace(season_number=1, episode_number=2),
        ]
    )

    assert client.get_tv_episodes(item_id="series", media_source=MediaSource.TMDB, media_id="1") == (
        "series",
        {1: [2]},
    )


@pytest.mark.parametrize("method", ["get_latest", "get_latest_backdrops"])
def test_latest_rejects_none_count_like_previous_arithmetic(method):
    """
    最新列表的 None 数量仍在原有乘法位置报告 TypeError。
    """
    client = _authenticated_client()

    with pytest.raises(TypeError, match="NoneType"):
        getattr(client, method)(num=None)


def test_get_items_rejects_none_start_index_like_previous_arithmetic():
    """
    媒体列表的 None 起始位置仍保持原有加法失败语义。
    """
    client = _authenticated_client()

    with pytest.raises(TypeError, match="NoneType"):
        list(client.get_items("library", start_index=None))


def test_get_resume_builds_play_item_and_get_image_cookies(monkeypatch):
    """
    继续观看条目应生成播放卡片，图片请求应返回带访问码的 Cookie。
    """
    client = _authenticated_client()
    client._api.play_list = Mock(
        return_value=[
            trimemedia_api.Item(
                guid="movie",
                type=trimemedia_api.Type.MOVIE,
                title="电影",
                poster="/poster.jpg",
            )
        ]
    )

    resume = client.get_resume(num=1)
    assert resume and resume[0].id == "movie"
    monkeypatch.setattr("app.modules.trimemedia.trimemedia.SecurityUtils.is_safe_url", lambda *_args, **_kwargs: True)
    assert client.get_image_cookies("https://trimemedia.example/poster.jpg") == {
        "Trim-MC-token": "token",
        "Trim-MC-access": "access",
    }


def test_module_image_cookies_returns_none_when_no_instance_owns_image(monkeypatch):
    """
    所有飞牛实例均拒绝图片地址时，模块不提供其它实例的授权凭据。

    :param monkeypatch: 隔离模块实例目录
    """
    module = TrimeMediaModule()
    server = SimpleNamespace(get_image_cookies=Mock(return_value=None))
    monkeypatch.setattr(module, "get_instances", lambda: {"first": server})

    assert module.mediaserver_image_cookies(image_url="https://other.example/poster.jpg") is None
    server.get_image_cookies.assert_called_once_with("https://other.example/poster.jpg")
