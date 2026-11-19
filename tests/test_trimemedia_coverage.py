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
from app.schemas.types import MediaSource, MediaType


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


def test_get_tv_episodes_returns_not_found_when_series_search_is_empty():
    """
    飞牛标题搜索成功但无匹配时，应按未入库返回空集映射。
    """
    client = _authenticated_client()
    client._api.search_list = Mock(return_value=[])

    assert client.get_tv_episodes(title="未收录的剧集", year="2026") == (None, {})
    client._api.search_list.assert_called_once_with(keywords="未收录的剧集")


def test_media_exists_allows_subscription_search_when_series_is_absent():
    """
    飞牛剧集搜索确认无结果时，严格媒体库查询应允许订阅继续搜索。
    """
    client = _authenticated_client()
    client._api.search_list = Mock(return_value=[])
    module = TrimeMediaModule.__new__(TrimeMediaModule)
    module._instances = {"PwStation": client}
    mediainfo = SimpleNamespace(
        type=MediaType.TV,
        title="未收录的剧集",
        year="2026",
        title_year="未收录的剧集 (2026)",
        media_source=MediaSource.TMDB,
        media_id="297253",
    )

    assert module.media_exists(
        mediainfo, server="PwStation", raise_on_query_failure=True
    ) is None


def test_get_tv_episodes_returns_not_found_when_stale_series_disappears():
    """
    飞牛缓存条目失效且标题搜索确认无结果时，应按未入库返回空映射。
    """
    client = _authenticated_client()
    client.get_iteminfo = Mock(return_value=None)
    client._api.search_list = Mock(return_value=[])

    assert client.get_tv_episodes(item_id="stale-series", title="已移除剧集") == (None, {})
    assert client._api.search_list.call_count == 1


def test_get_tv_episodes_preserves_series_search_failure():
    """
    飞牛电视剧搜索请求失败时，应保留查询失败状态。
    """
    client = _authenticated_client()
    client._api.search_list = Mock(return_value=None)

    assert client.get_tv_episodes(title="查询失败的剧集", year="2026") == (None, None)


def test_get_tv_episodes_returns_not_found_when_title_does_not_match():
    """
    飞牛搜索成功但剧名与年份均不匹配时，应按目标剧未入库处理。
    """
    client = _authenticated_client()
    client._api.search_list = Mock(
        return_value=[
            trimemedia_api.Item(
                guid="other-series",
                type=trimemedia_api.Type.TV,
                title="另一部剧集",
                air_date="2026-01-01",
            )
        ]
    )

    assert client.get_tv_episodes(title="未收录的剧集", year="2026") == (None, {})


def test_get_tv_episodes_preserves_season_query_failure():
    """
    飞牛季列表请求失败时，不应把未知集数转换为未入库。
    """
    client = _authenticated_client()
    client.get_iteminfo = Mock(
        return_value=SimpleNamespace(media_source=None, media_id=None)
    )
    client._api.season_list = Mock(return_value=None)

    assert client.get_tv_episodes(item_id="series") == (None, None)


def test_get_tv_episodes_preserves_episode_query_failure():
    """
    飞牛分集列表请求失败时，不应把未知集数转换为未入库。
    """
    client = _authenticated_client()
    client.get_iteminfo = Mock(
        return_value=SimpleNamespace(media_source=None, media_id=None)
    )
    client._api.season_list = Mock(
        return_value=[SimpleNamespace(guid="season", season_number=1)]
    )
    client._api.episode_list = Mock(return_value=None)

    assert client.get_tv_episodes(item_id="series") == (None, None)


def test_get_tv_episodes_returns_empty_map_when_series_has_no_seasons():
    """
    飞牛季列表请求成功但无季时，应返回确认无已入库分集的空映射。
    """
    client = _authenticated_client()
    client.get_iteminfo = Mock(
        return_value=SimpleNamespace(media_source=None, media_id=None)
    )
    client._api.season_list = Mock(return_value=[])

    assert client.get_tv_episodes(item_id="series") == ("series", {})


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


def test_verify_access_code_failure_log_omits_access_code(monkeypatch):
    """
    访问码是校验地址的路径段，连接失败时日志只记录设备根地址。

    :param monkeypatch: 隔离 HTTP 会话与日志输出
    """
    api = trimemedia_api.Api(
        host="https://trimemedia.example/v",
        apikey="apikey",
        access_code="device-access-code",
    )
    logs = Mock()
    monkeypatch.setattr(trimemedia_api, "logger", logs)
    monkeypatch.setattr(api._request_utils, "get_res", lambda *_args, **_kwargs: None)

    assert api.verify_access_code() is False
    message = logs.error.call_args[0][0]
    assert "device-access-code" not in message
    assert "https://trimemedia.example" in message
