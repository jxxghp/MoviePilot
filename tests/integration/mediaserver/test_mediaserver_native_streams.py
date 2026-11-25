"""
验证原生播放器按媒体服务器条目优先选流，单文件票据只读取本地媒体。
"""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from plexapi.video import Movie

from app.adapters import mediastream
from app.api.endpoints import mediaserver
from app.modules.emby.emby import Emby
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.navidrome import NavidromeModule
from app.modules.plex.plex import Plex
from app.schemas.mediaserver import MediaServerItem
from app.schemas.token import TokenPayload


@pytest.fixture
def playback_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    连接实际流路由和离线媒体服务器替身，本地与上游内容使用不同字节。

    :param monkeypatch: 自动还原路由和媒体服务器替身
    :param tmp_path: 隔离媒体文件目录
    :yield: 测试客户端、服务器链替身和本地媒体路径
    """
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"local-file")
    item = MediaServerItem(item_id="episode-1", item_type="Episode", title="第一集", path=str(path))
    chain = Mock()
    chain.find_item.return_value = ("Emby-客厅", item)
    chain.get_stream_url.return_value = "http://media.test/stream?server_key=synthetic-only"
    monkeypatch.setattr(mediaserver, "MediaServerChain", lambda: chain)
    monkeypatch.setattr(mediaserver, "_configured_stream_hosts", lambda *_args, **_kwargs: {"media.test"})
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=17)
    app.dependency_overrides[mediaserver.verify_resource_token] = lambda: TokenPayload(sub=17)
    with TestClient(app) as client:
        yield client, chain, path


def upstream_response(monkeypatch: pytest.MonkeyPatch, status_code: int, content_type: str = "video/mp4") -> Mock:
    """
    提供带 Range 响应头的离线 HTTP 上游，并保留请求以检查转发范围。

    :param monkeypatch: 自动恢复网络出口
    :param status_code: 上游响应码
    :param content_type: 上游媒体类型
    :return: HTTP 请求工具替身
    """
    response = Mock(status_code=status_code)
    response.headers = {
        "Content-Type": content_type, "Content-Length": "4",
        "Content-Range": "bytes 2-5/10", "Accept-Ranges": "bytes",
    }
    response.iter_content.return_value = [b"mote"]
    request = Mock()
    request.get_stream.return_value = nullcontext(response)
    monkeypatch.setattr(mediastream, "RequestUtils", Mock(return_value=request))
    return request


@pytest.mark.parametrize("local_file_exists", [True, False])
def test_library_stream_uses_server_before_local_file(
    monkeypatch: pytest.MonkeyPatch, playback_client, local_file_exists: bool
) -> None:
    """
    媒体服务器条目在本地有无同名文件时都优先读取上游，且不向 App 暴露服务凭据。
    """
    client, chain, path = playback_client
    if not local_file_exists:
        path.unlink()
    request = upstream_response(monkeypatch, 206)
    manifest_response = client.get(
        "/api/v1/mediaserver/stream-info", params={"itemid": "episode-1", "server": "Emby-客厅"}
    )
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()["data"]
    response = client.get(manifest["content_url"], headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"mote"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert "server_key" not in manifest_response.text
    assert str(path) not in manifest_response.text
    assert parse_qs(urlsplit(manifest["content_url"]).query) == {
        "itemid": ["episode-1"], "server": ["Emby-客厅"],
    }
    assert chain.find_item.call_args.kwargs["server"] == "Emby-客厅"
    chain.get_stream_url.assert_called_with("Emby-客厅", "episode-1", "Episode")
    assert request.get_stream.call_args.kwargs["headers"]["Range"] == "bytes=2-5"


def test_server_without_raw_stream_falls_back_to_local_file(playback_client) -> None:
    """
    适配器未实现原始文件流时，保留已授权条目的本地 Range 播放。
    """
    client, chain, _ = playback_client
    chain.get_stream_url.return_value = None
    response = client.get("/api/v1/mediaserver/stream", params={"itemid": "episode-1"}, headers={"Range": "bytes=0-4"})
    assert response.status_code == 206
    assert response.content == b"local"
    chain.get_stream_url.assert_called_once_with("Emby-客厅", "episode-1", "Episode")


@pytest.mark.parametrize("upstream_status", [404, 405, 501, 200])
def test_unsupported_server_stream_falls_back_to_local_range(
    monkeypatch: pytest.MonkeyPatch, playback_client, upstream_status: int
) -> None:
    """
    上游明确无流接口或忽略 Range 时，在响应发送前回退本地文件相同字节范围。
    """
    client, _, _ = playback_client
    upstream_response(monkeypatch, upstream_status)
    response = client.get("/api/v1/mediaserver/stream", params={"itemid": "episode-1"}, headers={"Range": "bytes=0-4"})
    assert response.status_code == 206
    assert response.content == b"local"
    assert response.headers["content-range"] == "bytes 0-4/10"


@pytest.mark.parametrize("upstream_status", [401, 403, 500, 416])
def test_authentication_or_server_errors_do_not_change_source(
    monkeypatch: pytest.MonkeyPatch, playback_client, upstream_status: int
) -> None:
    """
    鉴权、服务器错误与越界范围不被误判为缺少文件流接口。
    """
    client, _, _ = playback_client
    upstream_response(monkeypatch, upstream_status)
    response = client.get("/api/v1/mediaserver/stream", params={"itemid": "episode-1"}, headers={"Range": "bytes=0-4"})
    assert response.status_code == (416 if upstream_status == 416 else 502)
    assert response.content != b"local"


@pytest.mark.parametrize("content_type", ["text/html", "application/json", "application/xhtml+xml"])
def test_server_page_is_not_used_as_a_media_stream(
    monkeypatch: pytest.MonkeyPatch, playback_client, content_type: str
) -> None:
    """
    媒体服务器返回网页或 JSON 而非文件流时，不把错误页面送给播放器。
    """
    client, _, _ = playback_client
    upstream_response(monkeypatch, 206, content_type)
    response = client.get("/api/v1/mediaserver/stream", params={"itemid": "episode-1"}, headers={"Range": "bytes=0-4"})
    assert response.status_code == 206
    assert response.content == b"local"


def test_explicit_local_ticket_never_queries_media_server(monkeypatch: pytest.MonkeyPatch, playback_client) -> None:
    """
    文件管理和整理记录的单文件票据仅解析本地路径，即使存在同名媒体服务器条目。
    """
    client, chain, path = playback_client
    monkeypatch.setattr(mediaserver, "_resolve_file_stream_ticket", lambda *_args: path)
    monkeypatch.setattr(mediaserver, "_file_stream_ticket_media_type", lambda *_args: "video")
    response = client.get("/api/v1/mediaserver/stream", params={"itemid": "file.synthetic-ticket"}, headers={"Range": "bytes=0-4"})
    assert response.status_code == 206
    assert response.content == b"local"
    chain.find_item.assert_not_called()
    chain.get_stream_url.assert_not_called()


def test_music_metadata_mapping_can_stream_without_local_file(playback_client) -> None:
    """
    音乐元数据映射为服务器单曲后，无需 MoviePilot 挂载媒体服务器的磁盘路径。
    """
    client, chain, _ = playback_client
    chain.media_exists.return_value = SimpleNamespace(server="Navidrome", itemid="track-1")
    chain.iteminfo.return_value = MediaServerItem(item_id="track-1", item_type="音乐", title="单曲")
    response = client.post("/api/v1/mediaserver/stream-info/music", json={
        "media_source": "musicbrainz", "media_id": "recording-1", "title": "单曲", "artists": ["歌手"],
    })
    assert response.status_code == 200
    manifest = response.json()["data"]
    assert manifest["server"] == "Navidrome"
    assert manifest["media_type"] == "audio"
    assert manifest["lyrics_url"] is None
    assert manifest["subtitles"] == []
    chain.get_stream_url.assert_called_once_with("Navidrome", "track-1", "音乐")


@pytest.mark.parametrize("client_class,prefix", [(Emby, "/emby"), (Jellyfin, "")])
@pytest.mark.parametrize("item_type,route", [("Episode", "Videos"), ("Audio", "Audio"), ("音乐", "Audio")])
def test_emby_family_uses_actual_source_id_for_static_stream(
    monkeypatch: pytest.MonkeyPatch, client_class, prefix: str, item_type: str, route: str
) -> None:
    """
    Emby 与 Jellyfin 原始流使用实际媒体源 ID 和静态流参数，不构造网页播放地址。
    """
    service = client_class.__new__(client_class)
    service._host = "http://media.test/"
    service._apikey = "synthetic-key"
    service.user = "test-user"
    response = Mock(status_code=200)
    response.json.return_value = {"MediaSources": [{"Id": "actual-source-9"}]}
    request = Mock(get_res=Mock(return_value=response))
    monkeypatch.setattr(f"{client_class.__module__}.RequestUtils", Mock(return_value=request))
    url = service.get_stream_url("item-1", item_type)
    assert urlsplit(url).path == f"{prefix}/{route}/item-1/stream"
    query = parse_qs(urlsplit(url).query)
    assert query["static"] == ["true"]
    assert query["MediaSourceId"] == ["actual-source-9"]
    assert len(query["PlaySessionId"][0]) == 32
    assert query["api_key"] == ["synthetic-key"]
    assert request.get_res.call_args.args[0].endswith("/Items/item-1/PlaybackInfo")


def test_navidrome_module_accepts_unified_music_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Navidrome 使用统一中文音乐类型时仍返回 Subsonic 签名文件流。
    """
    module = NavidromeModule.__new__(NavidromeModule)
    service = Mock(get_play_url=Mock(return_value="https://music.test/rest/stream?id=track-1"))
    monkeypatch.setattr(module, "get_instance", Mock(return_value=service))

    assert module.mediaserver_stream_url("Navidrome", "track-1", "音乐") == (
        "https://music.test/rest/stream?id=track-1"
    )
    service.get_play_url.assert_called_once_with("track-1")


def test_plex_uses_original_part_url_instead_of_web_or_transcoding_page() -> None:
    """
    Plex 读取唯一媒体文件部分的原始地址，不使用跳转页或转码链路。
    """
    service = Plex.__new__(Plex)
    service._host = "http://media.test/"
    service._token = "synthetic"
    item = Mock(spec=Movie)
    item.iterParts.return_value = [SimpleNamespace(key="/library/parts/17/file.mkv")]
    service._plex = Mock()
    service._plex.fetchItem.return_value = item
    assert service.get_stream_url("/library/metadata/9") == (
        "http://media.test/library/parts/17/file.mkv?X-Plex-Token=synthetic"
    )
    service._plex.fetchItem.assert_called_once_with("/library/metadata/9")


def test_emby_strm_item_exposes_direct_strm_url_without_server_key(
    monkeypatch: pytest.MonkeyPatch, playback_client
) -> None:
    """
    验证 Emby 系 STRM 条目通过详情与 PlaybackInfo 提取客户端直链，且绝不泄露服务端 api_key。
    """
    from app.modules._base.embybrowse import EmbyBrowseConfig, emby_item_detail, emby_stream_url

    item_payload = {
        "Id": "strm-ep-1",
        "Name": "云盘直链第一集",
        "Type": "Episode",
        "Path": "/media/tv/Show/S01E01.strm",
        "MediaSources": [
            {
                "Id": "strm-src-1",
                "Container": "strm",
                "Protocol": "File",
                "Path": "/media/tv/Show/S01E01.strm",
                "MediaStreams": [],
            }
        ],
    }
    playback_payload = {
        "MediaSources": [
            {
                "Id": "strm-src-1",
                "Container": "mkv",
                "Protocol": "Http",
                "Path": "https://cdn.example.test/d/115/Show S01E01.mkv?sign=abc|User-Agent=Mozilla",
            }
        ]
    }

    def fake_get_res(url: str, _params: dict[str, object]) -> Mock:
        resp = Mock(status_code=200)
        resp.json.return_value = playback_payload if url.endswith("/PlaybackInfo") else item_payload
        return resp

    cfg = EmbyBrowseConfig(
        host="http://emby.test/",
        api_key="secret-emby-key",
        user_id="u-1",
        server_type="emby",
        request_factory=lambda: Mock(get_res=Mock(side_effect=fake_get_res)),
    )
    detail = emby_item_detail(cfg, "strm-ep-1")
    assert detail is not None
    assert detail.media_sources[0].direct_url == "https://cdn.example.test/d/115/Show%20S01E01.mkv?sign=abc"
    assert emby_stream_url(cfg, "strm-ep-1", "Episode") == "https://cdn.example.test/d/115/Show%20S01E01.mkv?sign=abc"

    client, chain, _ = playback_client
    chain.find_item.return_value = (
        "Emby-客厅",
        MediaServerItem(item_id="strm-ep-1", item_type="Episode", title="云盘直链第一集", path="/media/tv/Show/S01E01.strm"),
    )
    chain.item_detail.return_value = detail
    chain.get_stream_url.return_value = None

    manifest_response = client.get(
        "/api/v1/mediaserver/stream-info", params={"itemid": "strm-ep-1", "server": "Emby-客厅"}
    )
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()["data"]
    assert manifest["strm_url"] == "https://cdn.example.test/d/115/Show%20S01E01.mkv?sign=abc"
    assert "secret-emby-key" not in manifest_response.text
    assert "/media/tv/Show" not in manifest_response.text
