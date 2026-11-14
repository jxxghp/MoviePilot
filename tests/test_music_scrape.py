"""
验证音乐批量刮削的曲目字段、专辑信息及封面复用。
"""

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.application.configuration import get_chain_runtime_config_snapshot
from app.chain.scraping import ScrapingChain, ScrapingConfig, _MusicScrapeFileResult
from app.domain.context import MUSIC_ENTITY_ALBUM, MusicAlbumInfo, MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic
from app.runtime.events import Event
from app.schemas.file import FileItem
from app.schemas.types import EventType, ScrapingPolicy


def _media_chain() -> ScrapingChain:
    """
    构造不注册全局单例的音乐刮削链测试实例。
    """
    return object.__new__(ScrapingChain)


def _album_info() -> MusicInfo:
    """
    构造专辑批量刮削使用的标准目标。
    """
    return MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        artists=["周杰伦"],
        album="叶惠美",
        album_artist="周杰伦",
        year=2003,
        total_tracks=11,
        cover_url="https://example.com/album.jpg",
    )


def test_album_scrape_merge_preserves_track_fields_and_applies_album_metadata() -> None:
    """
    专辑批量刮削应保留每首歌自己的曲名和曲序，只统一专辑级字段。
    """
    local = MetaMusic(
        title="晴天",
        artists=["周杰伦"],
        album="错误专辑",
        album_artist="错误艺术家",
        year=1999,
        track_number=3,
        total_tracks=99,
    )

    merged = ScrapingChain._merge_music_album_metadata(local, _album_info())

    assert merged.title == "晴天"
    assert merged.artists == ["周杰伦"]
    assert merged.track_number == 3
    assert merged.album == "叶惠美"
    assert merged.album_artist == "周杰伦"
    assert merged.year == 2003
    assert merged.total_tracks == 11
    assert merged.media_source is None
    assert merged.media_id is None


def test_album_scrape_merge_prefers_online_total_discs() -> None:
    """
    专辑级合并应以线上碟数为准，线上缺失时才保留本地值。
    """
    album = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        artists=["周杰伦"],
        album="叶惠美",
        album_artist="周杰伦",
        year=2003,
        total_tracks=11,
        total_discs=1,
    )
    local = MetaMusic(title="晴天", artists=["周杰伦"], album="错误专辑", total_discs=2)

    merged = ScrapingChain._merge_music_album_metadata(local, album)

    assert merged.total_discs == 1
    assert merged.album == "叶惠美"
    assert merged.title == "晴天"

    album_without_discs = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        album="叶惠美",
        year=2003,
    )
    fallback = ScrapingChain._merge_music_album_metadata(local, album_without_discs)
    assert fallback.total_discs == 2


def test_album_scrape_merge_single_disc_forces_disc_number_one() -> None:
    """
    线上确认为单碟专辑时，本地错标的碟号不能与 total_discs=1 配成 "2/1"。
    """
    album = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        album="叶惠美",
        year=2003,
        total_tracks=11,
        total_discs=1,
    )
    local = MetaMusic(title="晴天", artists=["周杰伦"], disc_number=2, total_discs=2)

    merged = ScrapingChain._merge_music_album_metadata(local, album)

    assert (merged.disc_number, merged.total_discs) == (1, 1)

    multi_disc = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        album="叶惠美",
        year=2003,
        total_tracks=22,
        total_discs=2,
    )
    kept = ScrapingChain._merge_music_album_metadata(local, multi_disc)

    assert (kept.disc_number, kept.total_discs) == (2, 2)


def test_album_directory_scrape_processes_each_track_and_reuses_cover() -> None:
    """
    显式选择专辑刮削目录时应逐曲写标签，并让整批文件共用一次封面下载。
    """
    chain = _media_chain()
    chain.storagechain = Mock()
    chain.scraping_policies = Mock()

    def scraping_option(_target, metadata):
        """
        保持原测试只验证标签和封面，歌词由独立用例覆盖。
        """
        return SimpleNamespace(
            is_skip=metadata == "lyrics",
            is_overwrite=False,
        )

    chain.scraping_policies.option.side_effect = scraping_option
    audio_files = [
        FileItem(storage="local", path="/music/叶惠美/01.flac", type="file", name="01.flac"),
        FileItem(storage="local", path="/music/叶惠美/02.m4a", type="file", name="02.m4a"),
    ]
    chain.storagechain.list_files.return_value = audio_files
    chain._download_music_cover = Mock(return_value=(b"cover", "image/jpeg"))
    chain._scrape_music_file = Mock(
        return_value=_MusicScrapeFileResult(metadata_success=True)
    )
    album = _album_info()

    with patch(
        "app.chain.scraping.MediaChain.get_music_album",
        return_value=None,
    ) as get_music_album:
        success, message = chain.scrape_music_metadata(
            FileItem(
                storage="local",
                path="/music/叶惠美",
                type="dir",
                name="叶惠美",
            ),
            mediainfo=album,
        )

    assert success is True
    assert message == "已刮削 2 个音频文件"
    chain._download_music_cover.assert_called_once_with(album.cover_url)
    get_music_album.assert_called_once_with(
        media_source=album.media_source,
        media_id=album.media_id,
    )
    assert chain._scrape_music_file.call_count == 2
    assert all(
        call.args[1] is album and call.kwargs["cover"] == (b"cover", "image/jpeg")
        for call in chain._scrape_music_file.call_args_list
    )


def test_music_cover_download_uses_bounded_external_response_cache() -> None:
    """
    重复刮削同一封面时应复用缓存内容，避免再次访问外部图片接口。
    """
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "image/webp"},
        content=b"cover",
        close=Mock(),
    )
    request = Mock()
    request.get_res.return_value = response
    _clear_music_cover_cache()

    with patch("app.chain.scraping._scraping_http_snapshot") as snapshot:
        snapshot.return_value.get.return_value = response
        first = ScrapingChain._download_music_cover("https://example.com/album.webp")
        second = ScrapingChain._download_music_cover("https://example.com/album.webp")

    assert first == second == (b"cover", "image/webp")
    snapshot.return_value.get.assert_called_once_with(
        "https://example.com/album.webp",
        proxies=None,
        ua=get_chain_runtime_config_snapshot().normal_user_agent,
        timeout=20,
    )
    response.close.assert_called_once()
    _clear_music_cover_cache()


def _clear_music_cover_cache() -> None:
    """
    清空音乐封面有界缓存，保证各用例相互隔离。

    缓存装饰器经 staticmethod 包装后 mypy 无法识别 cache_clear，
    集中在此处忽略一次，避免每个用例重复出现同类诊断。
    """
    ScrapingChain._request_music_cover.cache_clear()  # type: ignore[attr-defined]


def _music_cover_response(status_code: int, content: bytes = b"", content_type: str = "image/jpeg") -> SimpleNamespace:
    """
    构造音乐封面下载的模拟 HTTP 响应。
    """
    return SimpleNamespace(
        status_code=status_code,
        headers={"Content-Type": content_type},
        content=content,
        close=Mock(),
    )


def test_music_cover_front500_404_falls_back_to_front() -> None:
    """
    front-500 缩略图 404 时应回退请求 /front 原图，且重刮应命中已回退的缓存。
    """
    release_group_id = "ac2b0f62-9139-351c-8639-c5bac2a31a30"
    front500_url = f"https://coverartarchive.org/release-group/{release_group_id}/front-500"
    front_url = f"https://coverartarchive.org/release-group/{release_group_id}/front"
    not_found = _music_cover_response(404)
    original = _music_cover_response(200, b"front-cover")
    _clear_music_cover_cache()

    with patch("app.chain.scraping._scraping_http_snapshot") as snapshot:
        snapshot.return_value.get.side_effect = lambda url, **kwargs: not_found if url == front500_url else original
        first = ScrapingChain._download_music_cover(front500_url)
        second = ScrapingChain._download_music_cover(front500_url)

    assert first == second == (b"front-cover", "image/jpeg")
    assert [call.args[0] for call in snapshot.return_value.get.call_args_list] == [
        front500_url,
        front_url,
    ]
    not_found.close.assert_called_once()
    original.close.assert_called_once()
    _clear_music_cover_cache()


def test_music_cover_front500_200_does_not_request_front() -> None:
    """
    front-500 正常返回时不应多请求 /front 原图。
    """
    release_group_id = "ac2b0f62-9139-351c-8639-c5bac2a31a30"
    front500_url = f"https://coverartarchive.org/release-group/{release_group_id}/front-500"
    thumbnail = _music_cover_response(200, b"thumb500")
    _clear_music_cover_cache()

    with patch("app.chain.scraping._scraping_http_snapshot") as snapshot:
        snapshot.return_value.get.return_value = thumbnail
        result = ScrapingChain._download_music_cover(front500_url)

    assert result == (b"thumb500", "image/jpeg")
    snapshot.return_value.get.assert_called_once()
    assert snapshot.return_value.get.call_args.args[0] == front500_url
    thumbnail.close.assert_called_once()
    _clear_music_cover_cache()


def test_music_cover_front500_and_front_both_404_returns_none_and_logs(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    front-500 与 /front 均 404 时应返回空封面，且 404 分支应写日志便于定位。

    模块日志器是 propagate=False 的 LoggerManager，caplog 无法直接捕获，
    因此测试内替换为标准日志器断言日志内容；真实日志器实际输出已在
    本地运行中通过 stderr 核对（两条 404 info 均出现）。
    """
    release_group_id = "ac2b0f62-9139-351c-8639-c5bac2a31a30"
    front500_url = f"https://coverartarchive.org/release-group/{release_group_id}/front-500"
    not_found = _music_cover_response(404)
    _clear_music_cover_cache()
    monkeypatch.setattr("app.chain.scraping.logger", logging.getLogger("test.music.cover.404"))

    with (
        caplog.at_level(logging.INFO, logger="test.music.cover.404"),
        patch("app.chain.scraping._scraping_http_snapshot") as snapshot,
    ):
        snapshot.return_value.get.return_value = not_found
        result = ScrapingChain._download_music_cover(front500_url)

    assert result == (None, "image/jpeg")
    assert snapshot.return_value.get.call_count == 2
    assert "404" in caplog.text
    assert front500_url in caplog.text
    _clear_music_cover_cache()


def test_music_cover_front500_404_then_front_500_returns_none_and_warns(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    front-500 404 回退到 /front 后 /front 500 时应返回空封面并写 warning。

    与主链路行为一致：非 404 的失败不进入缓存，直接返回 None。
    """
    release_group_id = "ac2b0f62-9139-351c-8639-c5bac2a31a30"
    front500_url = f"https://coverartarchive.org/release-group/{release_group_id}/front-500"
    front_url = f"https://coverartarchive.org/release-group/{release_group_id}/front"
    not_found = _music_cover_response(404)
    server_error = _music_cover_response(500)
    _clear_music_cover_cache()
    monkeypatch.setattr("app.chain.scraping.logger", logging.getLogger("test.music.cover.500"))

    with (
        caplog.at_level(logging.WARNING, logger="test.music.cover.500"),
        patch("app.chain.scraping._scraping_http_snapshot") as snapshot,
    ):
        snapshot.return_value.get.side_effect = lambda url, **kwargs: not_found if url == front500_url else server_error
        result = ScrapingChain._download_music_cover(front500_url)

    assert result == (None, "image/jpeg")
    assert snapshot.return_value.get.call_count == 2
    assert "500" in caplog.text
    assert front_url in caplog.text
    _clear_music_cover_cache()


def test_music_cover_non_front500_404_does_not_fallback() -> None:
    """
    非 front-500 封面地址 404 时不应猜测回退地址，只返回空封面。
    """
    cover_url = "https://example.com/album.webp"
    not_found = _music_cover_response(404)
    _clear_music_cover_cache()

    with patch("app.chain.scraping._scraping_http_snapshot") as snapshot:
        snapshot.return_value.get.return_value = not_found
        result = ScrapingChain._download_music_cover(cover_url)

    assert result == (None, "image/jpeg")
    snapshot.return_value.get.assert_called_once()
    assert snapshot.return_value.get.call_args.args[0] == cover_url
    _clear_music_cover_cache()


def test_recording_identity_rejects_multi_track_directory_scrape() -> None:
    """
    单曲身份不得覆盖整目录，否则会把同一首歌的标签写到专辑内所有文件。
    """
    chain = _media_chain()
    chain.storagechain = Mock()
    chain.storagechain.list_files.return_value = [
        FileItem(storage="local", path="/music/01.flac", type="file"),
        FileItem(storage="local", path="/music/02.flac", type="file"),
    ]

    success, message = chain.scrape_music_metadata(
        FileItem(storage="local", path="/music", type="dir"),
        mediainfo=MusicInfo(title="晴天", music_type="recording"),
    )

    assert success is False
    assert message == "单曲音乐 ID 仅支持刮削单个音频文件，整目录请选择专辑"


def test_generic_scrape_dispatches_music_without_entering_video_handlers() -> None:
    """
    统一刮削入口应直接分派音乐，不能因音频后缀过滤或落入电视剧分支。
    """
    chain = _media_chain()
    chain.scrape_music_metadata = Mock(return_value=(True, "已刮削 1 个音频文件"))
    music = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
    )
    fileitem = FileItem(
        storage="local",
        path="/music/晴天.flac",
        type="file",
        name="晴天.flac",
        extension="flac",
    )

    result = chain.scrape_metadata(
        fileitem=fileitem,
        meta=MetaMusic(title="晴天", artists=["周杰伦"]),
        mediainfo=music,
        overwrite=False,
    )

    assert result == (True, "已刮削 1 个音频文件")
    chain.scrape_music_metadata.assert_called_once_with(
        fileitem=fileitem,
        mediainfo=music,
        overwrite=False,
    )


def test_default_scraping_config_enables_music_lyrics_quality_upgrade() -> None:
    """
    新安装和未保存过该字段的用户应默认升级歌词且不允许质量降级。
    """
    assert ScrapingConfig.get_default_config()["music_lyrics"] == ScrapingPolicy.UPGRADE


def test_album_track_match_uses_disc_track_title_and_duration() -> None:
    """
    整张专辑刮削时应把本地音轨绑定到对应 Recording，不能复用专辑级身份。
    """
    album = MusicAlbumInfo(
        media_source="musicbrainz",
        media_id="album-1",
        title="叶惠美",
        tracks=[
            MusicInfo(
                media_source="musicbrainz",
                media_id="recording-1",
                title="以父之名",
                artists=["周杰伦"],
                disc_number=1,
                track_number=1,
                duration=342,
            ),
            MusicInfo(
                media_source="musicbrainz",
                media_id="recording-3",
                title="晴天",
                artists=["周杰伦"],
                disc_number=1,
                track_number=3,
                duration=269,
            ),
        ],
    )

    matched = ScrapingChain._match_music_album_track(
        MetaMusic(
            title="03 - 晴天",
            artists=["周杰伦"],
            disc_number=1,
            track_number=3,
            duration=270,
        ),
        album,
    )

    assert matched is not None
    assert matched.media_id == "recording-3"
    assert matched.title == "晴天"


def test_music_scrape_can_run_lyrics_without_tags_or_cover() -> None:
    """
    标签和封面关闭时，歌词开关仍应独立驱动逐曲处理。
    """
    chain = _media_chain()
    chain.storagechain = Mock()
    chain.scraping_policies = Mock()

    def scraping_option(_target, metadata):
        """
        仅开启歌词的缺失刮削策略。
        """
        return SimpleNamespace(
            is_skip=metadata != "lyrics",
            is_overwrite=False,
        )

    chain.scraping_policies.option.side_effect = scraping_option
    chain._scrape_music_file = Mock(
        return_value=_MusicScrapeFileResult(
            metadata_success=True,
            lyrics_status="saved",
        )
    )
    music_chain = Mock()

    with patch("app.chain.scraping.LyricsChain", return_value=music_chain):
        success, message = chain.scrape_music_metadata(
            FileItem(
                storage="local",
                path="/music/晴天.flac",
                type="file",
                name="晴天.flac",
            ),
            mediainfo=MusicInfo(title="晴天", artists=["周杰伦"]),
            overwrite=False,
        )

    assert success is True
    assert message == (
        "已刮削 1 个音频文件，歌词新增 1 首、升级 0 首、已存在 0 首、"
        "防降级保护 0 首、未匹配 0 首"
    )
    call = chain._scrape_music_file.call_args
    assert call.kwargs["write_tags"] is False
    assert call.kwargs["with_cover"] is False
    assert call.kwargs["lyrics_chain"] is music_chain


def test_write_music_lyrics_sidecar_creates_same_name_lrc(tmp_path) -> None:
    """
    同步歌词应以 UTF-8 同名 LRC 文件写入音轨所在目录。
    """
    chain = _media_chain()
    chain.storagechain = Mock()
    audio_path = tmp_path / "晴天.flac"
    audio_path.write_bytes(b"audio")

    success = chain._write_music_lyrics_sidecar(
        fileitem=FileItem(
            storage="local",
            path=audio_path.as_posix(),
            type="file",
            name=audio_path.name,
        ),
        local_path=audio_path,
        lyrics=MusicLyrics(
            provider="lrclib",
            provider_id="1",
            synced_lyrics="[00:01.00]故事的小黄花",
        ),
        overwrite=False,
    )

    lyric_path = audio_path.with_suffix(".lrc")
    assert success is True
    assert lyric_path.read_text(encoding="utf-8") == "[00:01.00]故事的小黄花\n"


def test_missing_only_lyrics_skips_existing_sidecar(tmp_path) -> None:
    """
    仅缺失策略发现同名歌词后不得再次请求歌词源。
    """
    chain = _media_chain()
    existing = FileItem(storage="local", path=(tmp_path / "晴天.lrc").as_posix(), type="file")
    chain.storagechain = Mock()
    chain.storagechain.get_file_item.return_value = existing
    music_chain = Mock()
    lyrics_option = SimpleNamespace(is_skip=False)

    status = chain._scrape_music_lyrics(
        fileitem=FileItem(storage="local", path=(tmp_path / "晴天.flac").as_posix(), type="file"),
        local_path=tmp_path / "晴天.flac",
        scrape_info=MetaMusic(title="晴天", artists=["周杰伦"]),
        lyrics_option=lyrics_option,
        overwrite=False,
        lyrics_chain=music_chain,
        album_info=None,
    )

    assert status == "existing"
    music_chain.lyrics.assert_not_called()


def test_write_music_lyrics_sidecar_uploads_to_remote_audio_directory(tmp_path) -> None:
    """
    远端存储歌词应使用音轨父目录和同名文件上传。
    """
    chain = _media_chain()
    chain.storagechain = Mock()
    parent = FileItem(storage="u115", path="/Music/叶惠美", type="dir")
    chain.storagechain.get_parent_item.return_value = parent
    chain.storagechain.upload_file.return_value = FileItem(
        storage="u115",
        path="/Music/叶惠美/晴天.lrc",
        type="file",
    )
    local_path = tmp_path / "晴天.flac"
    local_path.write_bytes(b"audio")
    fileitem = FileItem(
        storage="u115",
        path="/Music/叶惠美/晴天.flac",
        type="file",
        name="晴天.flac",
    )

    success = chain._write_music_lyrics_sidecar(
        fileitem=fileitem,
        local_path=local_path,
        lyrics=MusicLyrics(provider="lrclib", synced_lyrics="[00:01.00]晴天"),
        overwrite=False,
    )

    assert success is True
    upload = chain.storagechain.upload_file.call_args
    assert upload.args[0] is parent
    assert upload.kwargs["new_name"] == "晴天.lrc"


def test_music_scrape_event_preserves_independent_policy_overwrite() -> None:
    """
    标签覆盖策略不得升级为全局覆盖并误覆盖缺失模式下的歌词。
    """
    chain = _media_chain()
    chain.scrape_music_metadata = Mock(return_value=(True, "done"))
    fileitem = FileItem(storage="local", path="/music/叶惠美", type="dir")
    mediainfo = MusicInfo(
        media_source="musicbrainz",
        media_id="album-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
    )

    chain.scrape_metadata_event(
        Event(
            event_type=EventType.MetadataScrape,
            event_data={
                "fileitem": fileitem,
                "mediainfo": mediainfo,
                "overwrite": False,
            },
        )
    )

    chain.scrape_music_metadata.assert_called_once_with(
        fileitem=fileitem,
        mediainfo=mediainfo,
        overwrite=False,
    )


def test_music_scrape_event_uses_only_batch_files_and_per_track_contexts() -> None:
    """
    自动刮削必须按成功文件清单处理，并保留批次内每首歌各自的身份。
    """
    chain = _media_chain()
    chain.storagechain = Mock()
    chain.scrape_music_metadata = Mock(return_value=(True, "done"))
    root = FileItem(storage="local", path="/music/叶惠美", type="dir")
    paths = ["/music/叶惠美/01 - 以父之名.flac", "/music/叶惠美/03 - 晴天.flac"]
    audio_files = [
        FileItem(
            storage="local",
            path=path,
            type="file",
            name=path.rsplit("/", 1)[-1],
            extension="flac",
        )
        for path in paths
    ]
    chain.storagechain.get_file_item.side_effect = audio_files
    recordings = [
        MusicInfo(media_source="musicbrainz", media_id="recording-1", title="以父之名"),
        MusicInfo(media_source="musicbrainz", media_id="recording-3", title="晴天"),
    ]

    chain.scrape_metadata_event(Event(
        event_type=EventType.MetadataScrape,
        event_data={
            "fileitem": root,
            "file_list": paths,
            "mediainfo": recordings[0],
            "file_contexts": [
                {"path": path, "mediainfo": recording}
                for path, recording in zip(paths, recordings)
            ],
            "overwrite": False,
        },
    ))

    chain.scrape_music_metadata.assert_called_once_with(
        fileitem=root,
        mediainfo=recordings[0],
        overwrite=False,
        audio_files=audio_files,
        media_by_path=dict(zip(paths, recordings)),
    )


def test_music_scrape_batch_applies_each_recording_to_its_own_file() -> None:
    """
    多首单曲批次应逐文件使用对应身份，不触发单曲覆盖整目录保护。
    """
    chain = _media_chain()
    chain.storagechain = Mock()
    chain.scraping_policies = Mock()
    chain.scraping_policies.option.side_effect = lambda _target, metadata: SimpleNamespace(
        is_skip=metadata == "lyrics",
        is_overwrite=False,
    )
    chain._scrape_music_file = Mock(
        return_value=_MusicScrapeFileResult(metadata_success=True)
    )
    files = [
        FileItem(storage="local", path="/music/01.flac", type="file", name="01.flac"),
        FileItem(storage="local", path="/music/02.flac", type="file", name="02.flac"),
    ]
    recordings = [
        MusicInfo(media_source="musicbrainz", media_id="recording-1", title="Track 1"),
        MusicInfo(media_source="musicbrainz", media_id="recording-2", title="Track 2"),
    ]

    success, message = chain.scrape_music_metadata(
        fileitem=FileItem(storage="local", path="/music", type="dir"),
        mediainfo=recordings[0],
        overwrite=False,
        audio_files=files,
        media_by_path=dict(zip((item.path for item in files), recordings)),
    )

    assert success is True
    assert message == "已刮削 2 个音频文件"
    assert [call.args[1] for call in chain._scrape_music_file.call_args_list] == recordings


def test_music_download_failure_is_attributed_only_to_enabled_outputs() -> None:
    """
    音频下载失败时只标记实际启用的标签、封面或歌词任务。
    """
    chain = _media_chain()
    chain.storagechain = Mock()
    chain.storagechain.download_file.return_value = None
    fileitem = FileItem(
        storage="local",
        path="/music/晴天.flac",
        type="file",
        name="晴天.flac",
    )
    lyrics_option = SimpleNamespace(is_skip=False)

    lyrics_only = chain._scrape_music_file(
        fileitem=fileitem,
        mediainfo=MusicInfo(title="晴天"),
        write_tags=False,
        tag_overwrite=False,
        with_cover=False,
        lyrics_option=lyrics_option,
        lyrics_chain=Mock(),
    )
    metadata_only = chain._scrape_music_file(
        fileitem=fileitem,
        mediainfo=MusicInfo(title="晴天"),
        write_tags=True,
        tag_overwrite=False,
        with_cover=False,
        lyrics_option=SimpleNamespace(is_skip=True),
        lyrics_chain=None,
    )

    assert lyrics_only.metadata_success is True
    assert lyrics_only.lyrics_status == "failed"
    assert metadata_only.metadata_success is False
    assert metadata_only.lyrics_status == "disabled"
