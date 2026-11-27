"""
验证媒体流的 Range、字幕文件边界和电视剧单集映射。
"""

import asyncio
import socket
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import URL
from starlette.requests import Request

from app.adapters import mediastream
from app.api.endpoints import mediaserver
from app.chain.mediaserver import MediaServerChain
from app.schemas.mediaserver import MediaServerItem, MediaServerPlayItem, MediaServerSeasonInfo
from app.schemas.tmdb import TmdbEpisode
from app.schemas.token import TokenPayload
from app.schemas.types import MediaSource


def test_episode_metadata_keeps_server_identity_and_season(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证集元数据仅沿用指定服务器系列的明确身份，特别篇不会被改成第一季。
    """
    chain = Mock()
    chain.find_item.return_value = (
        "Emby-客厅", MediaServerItem(item_id="series-id", media_source=MediaSource.TMDB, media_id="157336")
    )
    tmdb = Mock()
    tmdb.tmdb_episodes.return_value = [
        TmdbEpisode(episode_number=3, season_number=0, name="特别篇", still_path="/special.jpg", runtime=45)
    ]
    monkeypatch.setattr(mediaserver, "MediaServerChain", lambda: chain)
    monkeypatch.setattr(mediaserver, "TmdbChain", lambda: tmdb)
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=1)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/mediaserver/episode-metadata",
            params={"itemid": "series-id", "server": "Emby-客厅", "season": 0},
        )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data[0]["name"] == "特别篇"
    assert data[0]["still_path"] == "/special.jpg"
    assert chain.find_item.call_args.kwargs["server"] == "Emby-客厅"
    tmdb.tmdb_episodes.assert_called_once_with(tmdbid=157336, season=0)


@pytest.mark.parametrize("source,media_id", [(None, None), (MediaSource.Douban, "123")])
def test_episode_metadata_does_not_guess_a_different_identity(
    monkeypatch: pytest.MonkeyPatch, source: Optional[MediaSource], media_id: Optional[str]
) -> None:
    """
    没有明确 TMDB 身份时不按名称识别另一部作品，单集播放仍使用独立的原映射。
    """
    chain = Mock()
    chain.find_item.return_value = (
        "Emby", MediaServerItem(title="同名作品", media_source=source, media_id=media_id)
    )
    tmdb = Mock()
    monkeypatch.setattr(mediaserver, "MediaServerChain", lambda: chain)
    monkeypatch.setattr(mediaserver, "TmdbChain", tmdb)
    assert mediaserver.episode_metadata("series-id", season=1).data == []
    tmdb.assert_not_called()


def test_local_stream_response_returns_requested_range(tmp_path: Path) -> None:
    """
    验证本地媒体 Range 响应只读取请求范围并报告正确长度。
    """
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"0123456789")

    response = mediastream._file_stream_response(path, "bytes=3-6")

    async def read_body() -> bytes:
        """
        收集测试响应的有限字节块。
        """
        return b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 3-6/10"
    assert response.headers["content-length"] == "4"
    assert asyncio.run(read_body()) == b"3456"


def test_local_stream_response_rejects_unsatisfiable_range(tmp_path: Path) -> None:
    """
    验证超出文件长度的 Range 请求返回 416 和总长度。
    """
    path = tmp_path / "movie.mp4"
    path.write_bytes(b"123")

    with pytest.raises(HTTPException) as error:
        mediastream._file_stream_response(path, "bytes=4-")

    assert error.value.status_code == 416
    assert error.value.headers == {"Content-Range": "bytes */3"}


def test_stream_routes_connect_manifest_subtitles_and_range(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    验证媒体流清单、同名字幕与后续 Range 请求在 FastAPI 路由间保持一致。
    """
    video = tmp_path / "Episode.mkv"
    video.write_bytes(b"0123456789")
    (tmp_path / "Episode.zh.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8")
    def find_stream_item(item_id: str, server: Optional[str] = None) -> tuple[str, MediaServerItem, Path]:
        """
        要求流和字幕路由保留完整的 Plex 条目标识。
        """
        assert item_id == "/library/metadata/123"
        return (
            "Emby",
            MediaServerItem(
                server="emby",
                item_id=item_id,
                title="测试剧集",
                item_type="Episode",
                path=str(video),
            ),
            video,
        )

    monkeypatch.setattr(mediaserver, "_find_stream_item", find_stream_item)
    monkeypatch.setattr(mediaserver.MediaServerChain, "get_stream_url", lambda *_args: None)
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=1)
    app.dependency_overrides[mediaserver.verify_resource_token] = lambda: TokenPayload(sub=1)

    with TestClient(app) as client:
        manifest_response = client.get(
            "/api/v1/mediaserver/stream-info", params={"itemid": "/library/metadata/123"}
        )
        manifest = manifest_response.json()["data"]
        media_response = client.get(manifest["content_url"], headers={"Range": "bytes=3-6"})
        subtitle_response = client.get(manifest["subtitles"][0]["url"])

    assert manifest_response.status_code == 200
    assert manifest["item_id"] == "/library/metadata/123"
    assert str(video) not in manifest_response.text
    assert manifest["content_url"] == (
        "http://testserver/api/v1/mediaserver/stream?itemid=%2Flibrary%2Fmetadata%2F123&server=Emby"
    )
    assert manifest["subtitles"][0]["subtitle_id"] == "Episode.zh.srt"
    assert media_response.status_code == 206
    assert media_response.headers["content-range"] == "bytes 3-6/10"
    assert media_response.content == b"3456"
    assert subtitle_response.status_code == 200
    assert "测试" in subtitle_response.text


def test_file_stream_ticket_is_opaque_user_bound_and_invalidated_on_file_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    验证单文件票据隐藏路径、绑定签发用户，并在同路径文件被替换后失效。
    """
    media_path = tmp_path / "selected.mkv"
    media_path.write_bytes(b"original-video")
    monkeypatch.setattr(mediastream, "get_runtime_setting", lambda _key: "test-resource-secret")

    item_id = mediastream._create_file_stream_ticket(media_path, user_id=17)

    assert item_id.startswith("file.")
    assert str(media_path) not in item_id
    assert mediastream._resolve_file_stream_ticket(item_id, user_id=17) == media_path.resolve()
    with pytest.raises(HTTPException) as wrong_user:
        mediastream._resolve_file_stream_ticket(item_id, user_id=18)
    assert wrong_user.value.status_code == 404

    media_path.write_bytes(b"replacement-video-with-another-identity")
    with pytest.raises(HTTPException) as replaced_file:
        mediastream._resolve_file_stream_ticket(item_id, user_id=17)
    assert replaced_file.value.status_code == 404


def test_file_stream_ticket_route_mounts_only_the_selected_local_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    验证文件管理票据只播放已授权的本地单文件，并继续提供对应字幕和 Range。
    """
    video = tmp_path / "Selected Episode.mkv"
    video.write_bytes(b"0123456789")
    (tmp_path / "Selected Episode.zh.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n对应字幕\n", encoding="utf-8"
    )
    monkeypatch.setattr(mediastream, "get_runtime_setting", lambda _key: "test-resource-secret")
    monkeypatch.setattr(mediaserver, "_ensure_manage_storage_item", lambda fileitem, _user: fileitem)
    current_user = SimpleNamespace(id=17, is_superuser=True, permissions={})
    current_user_id = [17]
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.get_current_active_manage_user] = lambda: current_user
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=current_user_id[0])
    app.dependency_overrides[mediaserver.verify_resource_token] = lambda: TokenPayload(sub=current_user_id[0])

    with TestClient(app) as client:
        ticket_response = client.post(
            "/api/v1/mediaserver/stream-ticket",
            json={"file": {"storage": "local", "type": "file", "name": video.name, "path": str(video)}},
        )
        item_id = ticket_response.json()["data"]["item_id"]
        manifest_response = client.get("/api/v1/mediaserver/stream-info", params={"itemid": item_id})
        manifest = manifest_response.json()["data"]
        media_response = client.get(manifest["content_url"], headers={"Range": "bytes=2-5"})
        subtitle_response = client.get(manifest["subtitles"][0]["url"])
        current_user_id[0] = 18
        other_user_response = client.get("/api/v1/mediaserver/stream-info", params={"itemid": item_id})

    assert ticket_response.status_code == 200
    assert str(video) not in ticket_response.text
    assert manifest_response.status_code == 200
    assert manifest["item_id"] == item_id
    assert manifest["title"] == video.stem
    assert str(video) not in manifest_response.text
    assert media_response.status_code == 206
    assert media_response.content == b"2345"
    assert subtitle_response.status_code == 200
    assert "对应字幕" in subtitle_response.text
    assert other_user_response.status_code == 404


def test_audio_file_ticket_mounts_matching_lrc_and_range_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """验证单个音频票据只挂载精确同名 LRC 歌词并支持流式跳转。"""
    audio = tmp_path / "Selected Track.flac"
    audio.write_bytes(b"0123456789")
    lyrics = tmp_path / "Selected Track.lrc"
    lyrics.write_text("[00:00.00]第一句\n[00:01.20]第二句\n", encoding="utf-8")
    (tmp_path / "Selected Track.live.lrc").write_text("不挂载版本歌词\n", encoding="utf-8")
    monkeypatch.setattr(mediastream, "get_runtime_setting", lambda _key: "test-resource-secret")
    monkeypatch.setattr(mediaserver, "_ensure_manage_storage_item", lambda fileitem, _user: fileitem)
    current_user = SimpleNamespace(id=17, is_superuser=True, permissions={})
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.get_current_active_manage_user] = lambda: current_user
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=17)
    app.dependency_overrides[mediaserver.verify_resource_token] = lambda: TokenPayload(sub=17)

    with TestClient(app) as client:
        ticket = client.post(
            "/api/v1/mediaserver/stream-ticket",
            json={
                "file": {"storage": "local", "type": "file", "name": audio.name, "path": str(audio)},
                "media_type": "audio",
            },
        )
        item_id = ticket.json()["data"]["item_id"]
        manifest_response = client.get("/api/v1/mediaserver/stream-info", params={"itemid": item_id})
        manifest = manifest_response.json()["data"]
        audio_response = client.get(manifest["content_url"], headers={"Range": "bytes=3-6"})
        lyrics_response = client.get(manifest["lyrics_url"])

    assert ticket.status_code == 200
    assert manifest["media_type"] == "audio"
    assert manifest["subtitles"] == []
    assert manifest["lyrics_url"].endswith(f"/stream-lyrics?itemid={item_id}")
    assert audio_response.status_code == 206
    assert audio_response.content == b"3456"
    assert lyrics_response.status_code == 200
    assert "第一句" in lyrics_response.text
    assert "不挂载版本歌词" not in lyrics_response.text


def test_music_stream_info_matches_a_library_track_by_recording_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    验证音乐详情的来源 ID 被映射为媒体服务器单曲 ID 后才返回流地址。
    """
    audio = tmp_path / "晴天.flac"
    audio.write_bytes(b"audio-bytes")
    (tmp_path / "晴天.lrc").write_text("[00:00.00]晴天\n", encoding="utf-8")
    chain = mediaserver.MediaServerChain
    monkeypatch.setattr(chain, "get_stream_url", lambda *_args: None)
    media_exists_calls: list[object] = []
    monkeypatch.setattr(
        chain,
        "media_exists",
        lambda _self, *, mediainfo, query: media_exists_calls.append(mediainfo)
        or SimpleNamespace(server="Navidrome", itemid="server-audio-id"),
    )
    monkeypatch.setattr(
        chain,
        "iteminfo",
        lambda _self, *, server, item_id, query: MediaServerItem(
            server=server,
            item_id=item_id,
            title="晴天",
            item_type="Audio",
            path=str(audio),
            note={"artist": "周杰伦", "album": "叶惠美"},
        ),
    )
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=17)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/mediaserver/stream-info/music",
            json={
                "media_source": "musicbrainz",
                "media_id": "recording-id",
                "title": "晴天",
                "artists": ["周杰伦"],
                "album": "叶惠美",
            },
        )

    manifest = response.json()["data"]
    assert response.status_code == 200
    assert media_exists_calls[0].media_id == "recording-id"
    assert manifest["item_id"] == "server-audio-id"
    assert manifest["server"] == "Navidrome"
    assert manifest["content_url"].endswith("stream?itemid=server-audio-id&server=Navidrome")
    assert manifest["media_type"] == "audio"
    assert manifest["artist"] == "周杰伦"
    assert manifest["album"] == "叶惠美"
    assert manifest["lyrics_url"].endswith("stream-lyrics?itemid=server-audio-id&server=Navidrome")


def test_episode_route_returns_only_available_season_episode_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证季集清单跳过缺集，并保留特别篇及实际媒体服务器条目标识。"""
    requested_seasons: list[int] = []
    chain = mediaserver.MediaServerChain
    monkeypatch.setattr(
        chain,
        "find_item",
        lambda _self, item_id, *, query: (
            "Emby",
            MediaServerItem(server="emby", item_id="series-id", title="测试剧集", item_type="Series"),
        ),
    )
    monkeypatch.setattr(
        chain,
        "episodes",
        lambda _self, *, server, item_id, query: [
            MediaServerSeasonInfo(season=0, episodes=[1]),
            MediaServerSeasonInfo(season=1, episodes=[1, 3]),
        ],
    )

    def season_episode_ids(_self, *, server: str, item_id: str, season: int) -> dict[int, str]:
        """記錄請求的季號並返回媒體服務器中的實際集 ID。"""
        assert item_id == "series-id"
        requested_seasons.append(season)
        return {0: {1: "special-id"}, 1: {1: "episode-1", 2: "missing-episode", 3: "episode-3"}}[season]

    monkeypatch.setattr(chain, "get_season_episode_ids", season_episode_ids)

    result = mediaserver.episode_ids("series-id")

    assert requested_seasons == [0, 1]
    assert result.data == {0: {1: "special-id"}, 1: {1: "episode-1", 3: "episode-3"}}


def test_subtitle_discovery_limits_to_matching_names(tmp_path: Path) -> None:
    """
    验证清单包含精确同名和语言后缀字幕，但不包含相似标题文件。
    """
    video = tmp_path / "Movie.mkv"
    exact = tmp_path / "Movie.srt"
    localized = tmp_path / "Movie.zh-CN.ass"
    unrelated = tmp_path / "Movie-making.srt"
    for path in (video, exact, localized, unrelated):
        path.write_text("fixture", encoding="utf-8")
    request = SimpleNamespace(
        url_for=lambda _name: URL("http://moviepilot.local/subtitles")
    )

    tracks = mediastream._subtitle_tracks("movie-id", video, request)  # type: ignore[arg-type]

    assert [track.subtitle_id for track in tracks] == ["Movie.srt", "Movie.zh-CN.ass"]
    assert tracks[1].language == "zh-CN"
    assert all("/tmp/" not in track.url for track in tracks)


def test_subtitle_discovery_rejects_links_outside_media_directory(tmp_path: Path) -> None:
    """
    验证名称匹配的字幕符号链接不能把媒体流接口引向影片目录以外。
    """
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    video = media_dir / "Movie.mkv"
    video.write_bytes(b"video")
    subtitle = media_dir / "Movie.srt"
    external = tmp_path / "secret.srt"
    external.write_text("not a media subtitle", encoding="utf-8")
    subtitle.symlink_to(external)
    request = SimpleNamespace(url_for=lambda _name: URL("http://moviepilot.local/subtitles"))

    tracks = mediastream._subtitle_tracks("movie-id", video, request)  # type: ignore[arg-type]

    assert tracks == []
    with pytest.raises(HTTPException) as error:
        mediastream._find_subtitle_path(video, "Movie.srt")
    assert error.value.status_code == 404


def test_empty_file_without_range_returns_empty_body_and_length(tmp_path: Path) -> None:
    """
    验证空文件仍返回零长度响应，不会报告不存在的第 0 个字节。
    """
    path = tmp_path / "empty.srt"
    path.touch()

    response = mediastream._file_stream_response(path, None)

    async def read_body() -> bytes:
        """
        收集测试响应的有限字节块。
        """
        return b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 200
    assert response.headers["content-length"] == "0"
    assert asyncio.run(read_body()) == b""


def test_strm_rejects_unconfigured_private_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证 STRM 不能把播放代理指向未配置的私有网络服务。
    """
    path = tmp_path / "Movie.strm"
    path.write_text("http://192.168.1.15/video.mkv", encoding="utf-8")
    monkeypatch.setattr(
        mediastream.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.15", 80))],
    )

    with pytest.raises(HTTPException) as error:
        mediastream._resolve_strm_source(path, set())

    assert error.value.status_code == 501
    assert "192.168.1.15" not in str(error.value.detail)


def test_local_strm_ticket_returns_client_direct_url_while_proxy_blocks_unconfigured_lan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    验证本地 STRM 文件票据向客户端返回无凭据 HTTP(S) 直链与同名字幕，同时服务端代理仍拦截未配置内网地址。
    """
    strm_file = tmp_path / "CloudMovie.strm"
    strm_file.write_text("#EXTM3U\nhttp://192.168.1.15:5244/d/115/Cloud Movie.mkv\n", encoding="utf-8-sig")
    (tmp_path / "CloudMovie.zh.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n外挂字幕\n", encoding="utf-8")
    credential_strm = tmp_path / "Secret.strm"
    credential_strm.write_text("http://user:secret@192.168.1.15:5244/video.mkv\n", encoding="utf-8")

    monkeypatch.setattr(mediastream, "get_runtime_setting", lambda _key: "test-resource-secret")
    monkeypatch.setattr(mediaserver, "_ensure_manage_storage_item", lambda fileitem, _user: fileitem)
    monkeypatch.setattr(
        mediastream.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.15", 5244))],
    )
    current_user = SimpleNamespace(id=17, is_superuser=True, permissions={})
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.get_current_active_manage_user] = lambda: current_user
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=17)
    app.dependency_overrides[mediaserver.verify_resource_token] = lambda: TokenPayload(sub=17)

    with TestClient(app) as client:
        ticket_resp = client.post(
            "/api/v1/mediaserver/stream-ticket",
            json={"file": {"storage": "local", "type": "file", "name": strm_file.name, "path": str(strm_file)}},
        )
        item_id = ticket_resp.json()["data"]["item_id"]
        manifest_resp = client.get("/api/v1/mediaserver/stream-info", params={"itemid": item_id})
        manifest = manifest_resp.json()["data"]
        proxy_resp = client.get(manifest["content_url"])

        cred_ticket = client.post(
            "/api/v1/mediaserver/stream-ticket",
            json={"file": {"storage": "local", "type": "file", "name": credential_strm.name, "path": str(credential_strm)}},
        ).json()["data"]["item_id"]
        cred_manifest_resp = client.get("/api/v1/mediaserver/stream-info", params={"itemid": cred_ticket})

    assert manifest_resp.status_code == 200
    assert manifest["strm_url"] == "http://192.168.1.15:5244/d/115/Cloud%20Movie.mkv"
    assert len(manifest["subtitles"]) == 1
    assert proxy_resp.status_code == 501
    assert cred_manifest_resp.status_code == 501
    assert "secret" not in cred_manifest_resp.text


def test_configured_private_stream_host_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证管理员配置过的私有媒体主机无需公网 DNS 也可作为 STRM 来源。"""
    hosts = mediastream._configured_stream_hosts(
        [SimpleNamespace(config={"api_url": "http://192.168.1.15:32400"})],
        [SimpleNamespace(config={"endpoint": "https://cloud.example:443"})],
    )
    monkeypatch.setattr(mediastream.socket, "getaddrinfo", Mock(side_effect=AssertionError("不应查询 DNS")))

    result = mediastream._validate_stream_url("http://192.168.1.15/video.mkv", configured_hosts=hosts)

    assert result == "http://192.168.1.15/video.mkv"
    assert hosts == {"192.168.1.15", "cloud.example"}


def test_episode_endpoint_returns_single_episode_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证电视剧季集查询返回可播放集的媒体服务器条目标识。
    """
    chain = Mock()
    chain.find_item.return_value = (
        "Emby",
        MediaServerItem(server="emby", item_id="series-id", item_type="Series"),
    )
    chain.get_season_episode_ids.return_value = {1: "episode-1", 3: "episode-3"}
    monkeypatch.setattr(mediaserver, "MediaServerChain", lambda: chain)

    response = mediaserver.season_episode_ids("series-id", 1)

    assert response.success
    assert response.data == {1: "episode-1", 3: "episode-3"}
    chain.get_season_episode_ids.assert_called_once_with(server="Emby", item_id="series-id", season=1)


def test_episode_inventory_returns_only_existing_media_server_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证剧集清单使用媒体服务器原生季集编号并保留每一集的实际条目标识。
    """
    chain = Mock()
    chain.find_item.return_value = (
        "Emby",
        MediaServerItem(server="emby", item_id="series-id", item_type="Series"),
    )
    chain.episodes.return_value = [
        MediaServerSeasonInfo(season=1, episodes=[1, 3]),
        MediaServerSeasonInfo(season=2, episodes=[1]),
    ]
    chain.get_season_episode_ids.side_effect = [{1: "s1e1", 2: "s1e2", 3: "s1e3"}, {1: "s2e1"}]
    monkeypatch.setattr(mediaserver, "MediaServerChain", lambda: chain)

    response = mediaserver.episode_ids("series-id")

    assert response.success
    assert response.data == {1: {1: "s1e1", 3: "s1e3"}, 2: {1: "s2e1"}}


def test_subtitle_endpoint_rejects_directory_fragments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    验证字幕端点拒绝带目录片段的字幕标识。
    """
    video = tmp_path / "Movie.mkv"
    video.write_text("video", encoding="utf-8")
    monkeypatch.setattr(mediaserver, "_find_stream_item", lambda _item_id: ("Emby", None, video))
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    with pytest.raises(HTTPException) as error:
        mediaserver.stream_item_subtitle("movie-id", "../Movie.srt", request, TokenPayload(sub=1))

    assert error.value.status_code == 404


def test_remote_stream_preserves_range_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证 STRM 上游收到 Range 并向播放器返回 206 及原始内容范围。
    """
    upstream = Mock(
        status_code=206,
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": "4",
            "Content-Range": "bytes 4-7/20",
            "Accept-Ranges": "bytes",
        },
    )
    upstream.iter_content.return_value = iter([b"data"])
    client = Mock(get_stream=Mock(return_value=nullcontext(upstream)))
    monkeypatch.setattr(mediastream, "RequestUtils", lambda **_kwargs: client)
    monkeypatch.setattr(mediastream, "_validate_stream_url", lambda url, **_kwargs: url)

    response = mediastream._remote_stream_response("https://media.example/video.mp4", "bytes=4-7", set())

    async def read_body() -> bytes:
        """
        收集测试响应的有限字节块。
        """
        return b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 4-7/20"
    assert asyncio.run(read_body()) == b"data"
    client.get_stream.assert_called_once()
    assert client.get_stream.call_args.kwargs["headers"]["Range"] == "bytes=4-7"
    client.close.assert_called_once()


def test_remote_stream_maps_upstream_range_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    上游 416 应转换为本地化的标准错误，同时保留播放器所需的范围头。
    """
    upstream = Mock(
        status_code=416,
        headers={"Content-Range": "bytes */20", "Accept-Ranges": "bytes"},
    )
    context = Mock()
    context.__enter__ = Mock(return_value=upstream)
    context.__exit__ = Mock(return_value=False)
    client = Mock(get_stream=Mock(return_value=context))
    monkeypatch.setattr(mediastream, "RequestUtils", lambda **_kwargs: client)
    monkeypatch.setattr(mediastream, "_validate_stream_url", lambda url, **_kwargs: url)

    with pytest.raises(HTTPException) as error:
        mediastream._remote_stream_response("https://media.example/video.mp4", "bytes=100-", set())

    assert error.value.status_code == 416
    assert error.value.headers == {"Content-Range": "bytes */20", "Accept-Ranges": "bytes"}
    context.__exit__.assert_called_once()
    client.close.assert_called_once()


def test_strm_connection_failure_closes_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    上游打开失败也必须释放当前请求创建的传输，且不回显带凭据的上游地址。
    """
    context = Mock()
    context.__enter__ = Mock(side_effect=ConnectionError("private-url-secret"))
    client = Mock(get_stream=Mock(return_value=context))
    monkeypatch.setattr(mediastream, "RequestUtils", lambda **_kwargs: client)
    monkeypatch.setattr(mediastream, "_validate_stream_url", lambda url, **_kwargs: url)

    with pytest.raises(HTTPException) as error:
        mediastream._open_upstream_stream("https://media.example/video?token=secret", None, set())

    assert error.value.status_code == 502
    assert "secret" not in error.value.detail
    client.close.assert_called_once()


def test_strm_redirect_rechecks_private_destination_and_closes_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    已配置的上游重定向到另一私有主机时拒绝第二跳并释放首跳连接及客户端。
    """
    upstream = Mock(status_code=302, headers={"Location": "http://127.0.0.2/private"})
    context = Mock()
    context.__enter__ = Mock(return_value=upstream)
    context.__exit__ = Mock(return_value=False)
    client = Mock(get_stream=Mock(return_value=context))
    monkeypatch.setattr(mediastream, "RequestUtils", lambda **_kwargs: client)

    with pytest.raises(HTTPException) as error:
        mediastream._open_upstream_stream("http://127.0.0.1/video", "bytes=0-", {"127.0.0.1"})

    assert error.value.status_code == 501
    client.get_stream.assert_called_once()
    context.__exit__.assert_called_once()
    client.close.assert_called_once()


def test_strm_rejects_upstream_without_range_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    上游忽略 Range 时明确拒绝跳转，不能把完整正文伪装成局部内容。
    """
    upstream = Mock(status_code=200, headers={"Content-Type": "video/mp4"})
    client = Mock(get_stream=Mock(return_value=nullcontext(upstream)))
    monkeypatch.setattr(mediastream, "RequestUtils", lambda **_kwargs: client)
    monkeypatch.setattr(mediastream, "_validate_stream_url", lambda url, **_kwargs: url)

    with pytest.raises(HTTPException) as error:
        mediastream._remote_stream_response("https://media.example/video", "bytes=100-", set())

    assert error.value.status_code == 501
    upstream.iter_content.assert_not_called()
    client.close.assert_called_once()


def test_media_server_search_endpoint_filters_types_and_caps_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证媒体库搜索向服务模块分发电影、电视剧和音乐类型并过滤无效类型。"""
    chain = Mock()
    chain.search_library.return_value = [
        MediaServerPlayItem(server="Emby-客厅", item_id="series-1", title="测试剧集", type="电视剧")
    ]
    monkeypatch.setattr(mediaserver, "MediaServerChain", lambda: chain)
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=1)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/mediaserver/search",
            params=[
                ("keyword", "测试剧集"),
                ("type", "电视剧"),
                ("type", "电影"),
                ("type", "音乐"),
                ("type", "无效类型"),
                ("limit", "45"),
            ],
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["server"] == "Emby-客厅"
    chain.search_library.assert_called_once_with("测试剧集", ["电视剧", "电影", "音乐"], 45)


def test_media_server_search_chain_deduplicates_per_server_and_signs_posters(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证跨服务器同 ID 条目不互相覆盖，重复结果只保留一项并签名海报。"""
    chain = MediaServerChain()
    run_module = Mock(
        return_value=[
            MediaServerPlayItem(server="Emby-A", item_id="same-id", title="剧集 A", type="电视剧", image="https://a/poster"),
            MediaServerPlayItem(server="Emby-A", item_id="same-id", title="重复项", type="电视剧"),
            MediaServerPlayItem(server="Emby-B", item_id="same-id", title="剧集 B", type="电视剧", image="https://b/poster"),
            MediaServerPlayItem(server="Emby-A", item_id="movie-id", title="电影", type="电影"),
        ]
    )
    monkeypatch.setattr(chain, "run_module", run_module)
    monkeypatch.setattr(chain, "_sign_image_url", lambda url: f"signed:{url}" if url else url)

    items = chain.search_library("剧集", ["电视剧", "音乐"], 20)

    assert [(item.server, item.item_id) for item in items] == [
        ("Emby-A", "same-id"),
        ("Emby-B", "same-id"),
    ]
    assert [item.image for item in items] == ["signed:https://a/poster", "signed:https://b/poster"]
    run_module.assert_called_once_with("mediaserver_search", keyword="剧集", types=["电视剧", "音乐"], limit=20)


@pytest.mark.parametrize(
    "filename,expected_mime",
    [
        ("track.flac", "audio/flac"),
        ("track.m4a", "audio/mp4"),
        ("audiobook.m4b", "audio/mp4"),
        ("track.mp3", "audio/mpeg"),
        ("track.wav", "audio/wav"),
        ("track.aac", "audio/aac"),
        ("track.mpc", "audio/x-musepack"),
        ("track.dsf", "audio/x-dsf"),
        ("track.ape", "audio/x-ape"),
    ],
)
def test_audio_file_stream_exposes_standard_mime_and_progressive_chunks(
    tmp_path: Path, filename: str, expected_mime: str
) -> None:
    """验证本地音频流返回标准 MIME 类型、64KB 渐进分块与 HEAD 元数据探测，支持边加载边播放。"""
    payload = b"A" * (140 * 1024)
    audio = tmp_path / filename
    audio.write_bytes(payload)

    head_resp = mediastream._file_stream_response(audio, None, head_only=True)
    assert head_resp.status_code == 200
    assert head_resp.media_type == expected_mime
    assert head_resp.headers["accept-ranges"] == "bytes"
    assert head_resp.headers["content-length"] == str(len(payload))

    async def collect(resp) -> list[bytes]:
        return [chunk async for chunk in resp.body_iterator]

    assert asyncio.run(collect(head_resp)) == []

    stream_resp = mediastream._file_stream_response(audio, "bytes=0-131071")
    chunks = asyncio.run(collect(stream_resp))
    assert stream_resp.status_code == 206
    assert stream_resp.media_type == expected_mime
    assert stream_resp.headers["content-range"] == f"bytes 0-131071/{len(payload)}"
    assert [len(c) for c in chunks] == [64 * 1024, 64 * 1024]


def test_stream_route_supports_head_and_range_for_local_audio_ticket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """验证 `/api/v1/mediaserver/stream` 支持 `HEAD` 与 `GET Range` 探测本地音频票据，不再返回 405。"""
    audio = tmp_path / "Lossless Track.m4a"
    audio.write_bytes(b"0123456789abcdef")
    monkeypatch.setattr(mediastream, "get_runtime_setting", lambda _key: "test-resource-secret")
    monkeypatch.setattr(mediaserver, "_ensure_manage_storage_item", lambda fileitem, _user: fileitem)
    current_user = SimpleNamespace(id=19, is_superuser=True, permissions={})
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.get_current_active_manage_user] = lambda: current_user
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=19)
    app.dependency_overrides[mediaserver.verify_resource_token] = lambda: TokenPayload(sub=19)

    with TestClient(app) as client:
        ticket = client.post(
            "/api/v1/mediaserver/stream-ticket",
            json={
                "file": {"storage": "local", "type": "file", "name": audio.name, "path": str(audio)},
                "media_type": "audio",
            },
        )
        item_id = ticket.json()["data"]["item_id"]
        manifest = client.get("/api/v1/mediaserver/stream-info", params={"itemid": item_id}).json()["data"]
        head_resp = client.head(manifest["content_url"])
        range_resp = client.get(manifest["content_url"], headers={"Range": "bytes=0-3"})

    assert head_resp.status_code == 200
    assert head_resp.headers["accept-ranges"] == "bytes"
    assert head_resp.headers["content-length"] == "16"
    assert head_resp.headers["content-type"].startswith("audio/mp4")
    assert head_resp.content == b""
    assert range_resp.status_code == 206
    assert range_resp.headers["content-range"] == "bytes 0-3/16"
    assert range_resp.content == b"0123"


def test_remote_storage_smb_and_http_progressive_stream_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 SMB（可随机定位流）与 OpenList/网盘（HTTP 直链）远端文件支持签发流票据并按 Range 边加载边播放。"""
    import io

    payload = b"0123456789abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setattr(mediastream, "get_runtime_setting", lambda _key: "test-resource-secret")
    monkeypatch.setattr(mediaserver, "_ensure_manage_storage_item", lambda fileitem, _user: fileitem)

    class _FakeStorageChain:
        def supports_file_stream(self, storage: str) -> bool:
            return storage in {"smb", "alist"}

        def get_file_stream_url(self, fileitem):
            if fileitem.storage == "alist":
                return ("https://cdn.example.com/movie.mp4", {"Referer": "https://alist.example.com/"})
            return None

        def open_file_stream(self, fileitem):
            if fileitem.storage == "smb":
                return io.BytesIO(payload), len(payload)
            return None

    monkeypatch.setattr(mediaserver, "StorageChain", _FakeStorageChain)
    monkeypatch.setattr(
        mediaserver,
        "_remote_stream_response",
        lambda url, range_header, _hosts, extra_headers=None, head_only=False: mediaserver.StreamingResponse(
            iter(() if head_only else [b"ABCD"]),
            status_code=206 if range_header else 200,
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes", "X-Upstream-Url": url, "X-Has-Referer": str(bool(extra_headers))},
        ),
    )

    current_user = SimpleNamespace(id=21, is_superuser=True, permissions={})
    app = FastAPI()
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[mediaserver.get_current_active_manage_user] = lambda: current_user
    app.dependency_overrides[mediaserver.verify_token] = lambda: TokenPayload(sub=21)
    app.dependency_overrides[mediaserver.verify_resource_token] = lambda: TokenPayload(sub=21)

    with TestClient(app) as client:
        # 1. SMB 可 seek 分块 Range 流式读取
        smb_ticket = client.post(
            "/api/v1/mediaserver/stream-ticket",
            json={
                "file": {"storage": "smb", "type": "file", "name": "demo.mp4", "path": "/share/demo.mp4"},
                "media_type": "video",
            },
        )
        assert smb_ticket.status_code == 200
        smb_id = smb_ticket.json()["data"]["item_id"]
        smb_manifest = client.get("/api/v1/mediaserver/stream-info", params={"itemid": smb_id}).json()["data"]
        smb_head = client.head(smb_manifest["content_url"])
        smb_range = client.get(smb_manifest["content_url"], headers={"Range": "bytes=10-15"})
        assert smb_head.status_code == 200
        assert smb_head.headers["content-length"] == str(len(payload))
        assert smb_range.status_code == 206
        assert smb_range.headers["content-range"] == f"bytes 10-15/{len(payload)}"
        assert smb_range.content == b"abcdef"

        # 2. OpenList HTTP 直链代理
        alist_ticket = client.post(
            "/api/v1/mediaserver/stream-ticket",
            json={
                "file": {"storage": "alist", "type": "file", "name": "movie.mp4", "path": "/media/movie.mp4"},
                "media_type": "video",
            },
        )
        assert alist_ticket.status_code == 200
        alist_id = alist_ticket.json()["data"]["item_id"]
        alist_manifest = client.get("/api/v1/mediaserver/stream-info", params={"itemid": alist_id}).json()["data"]
        alist_range = client.get(alist_manifest["content_url"], headers={"Range": "bytes=0-3"})
        assert alist_range.status_code == 206
        assert alist_range.headers["x-upstream-url"] == "https://cdn.example.com/movie.mp4"
        assert alist_range.content == b"ABCD"

        # 3. 不支持流式的存储返回 400
        unsupported = client.post(
            "/api/v1/mediaserver/stream-ticket",
            json={
                "file": {"storage": "rclone", "type": "file", "name": "movie.mp4", "path": "/media/movie.mp4"},
                "media_type": "video",
            },
        )
        assert unsupported.status_code == 400


def test_smb_open_file_stream_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证 SMB.open_file_stream 的空路径、正常打开与异常回退分支。
    """
    import io
    import sys
    import types

    from app.modules.filemanager.storages.smb import SMB
    from app.schemas.file import FileItem

    storage = SMB.__new__(SMB)
    storage._connected = False  # noqa: SLF001
    monkeypatch.setattr(storage, "_check_connection", lambda: None)
    monkeypatch.setattr(storage, "_normalize_path", lambda p: f"\\\\nas\\share\\{p.lstrip('/')}")

    fake_stream = io.BytesIO(b"hello-smb")
    fake_smbclient = types.SimpleNamespace(
        stat=lambda _p: SimpleNamespace(st_size=9),
        open_file=lambda _p, mode="rb": fake_stream if mode == "rb" else None,
    )
    monkeypatch.setitem(sys.modules, "smbclient", fake_smbclient)

    assert storage.open_file_stream(FileItem(storage="smb", type="file", path="", name="empty.mp4")) is None

    result = storage.open_file_stream(FileItem(storage="smb", type="file", path="/movies/a.mp4", name="a.mp4"))
    assert result == (fake_stream, 9)

    def _boom(_p: str):
        raise OSError("smb broken")

    monkeypatch.setitem(
        sys.modules,
        "smbclient",
        types.SimpleNamespace(stat=_boom, open_file=lambda _p, mode="rb": None),
    )
    assert storage.open_file_stream(FileItem(storage="smb", type="file", path="/movies/a.mp4", name="a.mp4")) is None


def test_emby_subtitle_url_supports_colon_in_media_source_id() -> None:
    """
    验证代理字幕标识中媒体源 ID 含有冒号时仍能准确解析上游字幕流地址。
    """
    from app.modules._base.embybrowse import EmbyBrowseConfig, emby_subtitle_url

    cfg = EmbyBrowseConfig(
        host="https://emby.example:8920/",
        api_key="emby-token",
        user_id="u1",
        server_type="emby",
    )
    url = emby_subtitle_url(cfg, "item-42", "ms:strm:source:99:3.ass")
    assert url == (
        "https://emby.example:8920/emby/Videos/item-42/"
        "strm%3Asource%3A99/Subtitles/3/Stream.ass?api_key=emby-token"
    )


def test_open_upstream_stream_retries_self_signed_tls_only_for_configured_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证管理员配置的媒体服务器遇到自签名证书 SSLError 时允许单跳回退，外部未配置主机仍严格拒绝。
    """
    import requests

    ssl_fail_ctx = Mock()
    ssl_fail_ctx.__enter__ = Mock(side_effect=requests.exceptions.SSLError("self-signed cert"))
    ssl_fail_ctx.__exit__ = Mock(return_value=False)

    ok_upstream = Mock(status_code=200, headers={"Content-Type": "text/x-ssa"})
    ok_ctx = nullcontext(ok_upstream)

    strict_client = Mock(get_stream=Mock(return_value=ssl_fail_ctx))
    insecure_client = Mock(get_stream=Mock(return_value=ok_ctx))

    def fake_request_utils(**kwargs):
        return strict_client if kwargs.get("verify", True) else insecure_client

    monkeypatch.setattr(mediastream, "RequestUtils", fake_request_utils)
    monkeypatch.setattr(mediastream, "_validate_stream_url", lambda url, **_kwargs: url)

    resp, _ctx, returned_client = mediastream._open_upstream_stream(
        "https://192.168.1.10:8920/emby/Subtitles/Stream.ass",
        None,
        {"192.168.1.10"},
    )
    assert resp is ok_upstream
    assert returned_client is insecure_client
    strict_client.close.assert_called_once()

    strict_client.reset_mock()
    insecure_client.reset_mock()
    with pytest.raises(HTTPException) as external_err:
        mediastream._open_upstream_stream(
            "https://cdn.external.example/sub.ass",
            None,
            {"192.168.1.10"},
        )
    assert external_err.value.status_code == 502
    insecure_client.get_stream.assert_not_called()
    strict_client.close.assert_called_once()
