from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.chain.media import MediaChain, ScrapingConfig, _MusicScrapeFileResult
from app.core.context import MUSIC_ENTITY_ALBUM, MusicAlbumInfo, MusicInfo, MusicLyrics
from app.core.event import Event
from app.core.meta import MetaMusic
from app.schemas import FileItem
from app.schemas.types import EventType, ScrapingPolicy


def _media_chain() -> MediaChain:
    """构造不注册全局单例的音乐刮削链测试实例。"""
    return object.__new__(MediaChain)


def _album_info() -> MusicInfo:
    """构造专辑批量刮削使用的标准目标。"""
    return MusicInfo(
        source="musicbrainz",
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


def test_album_scrape_merge_preserves_track_fields_and_applies_album_identity() -> None:
    """专辑批量刮削应保留每首歌自己的曲名和曲序，只统一专辑级字段。"""
    local = MetaMusic(
        title="晴天",
        artists=["周杰伦"],
        album="错误专辑",
        album_artist="错误艺术家",
        year=1999,
        track_number=3,
        total_tracks=99,
    )

    merged = MediaChain._merge_music_album_metadata(local, _album_info())

    assert merged.title == "晴天"
    assert merged.artists == ["周杰伦"]
    assert merged.track_number == 3
    assert merged.album == "叶惠美"
    assert merged.album_artist == "周杰伦"
    assert merged.year == 2003
    assert merged.total_tracks == 11
    assert merged.media_source == "musicbrainz"
    assert merged.media_id == "release-group-1"


def test_album_directory_scrape_processes_each_track_and_reuses_cover() -> None:
    """显式选择专辑刮削目录时应逐曲写标签，并让整批文件共用一次封面下载。"""
    chain = _media_chain()
    chain.storagechain = Mock()
    chain.scraping_policies = Mock()

    def scraping_option(_target, metadata):
        """保持原测试只验证标签和封面，歌词由独立用例覆盖。"""
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

    success, message = chain.scrape_music_metadata(
        FileItem(storage="local", path="/music/叶惠美", type="dir", name="叶惠美"),
        mediainfo=album,
    )

    assert success is True
    assert message == "已刮削 2 个音频文件"
    chain._download_music_cover.assert_called_once_with(album.cover_url)
    assert chain._scrape_music_file.call_count == 2
    assert all(
        call.args[1] is album and call.kwargs["cover"] == (b"cover", "image/jpeg")
        for call in chain._scrape_music_file.call_args_list
    )


def test_music_cover_download_uses_bounded_external_response_cache() -> None:
    """重复刮削同一封面时应复用缓存内容，避免再次访问外部图片接口。"""
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "image/webp"},
        content=b"cover",
        close=Mock(),
    )
    request = Mock()
    request.get_res.return_value = response
    MediaChain._request_music_cover.cache_clear()

    with patch("app.chain.media.RequestUtils", return_value=request):
        first = MediaChain._download_music_cover("https://example.com/album.webp")
        second = MediaChain._download_music_cover("https://example.com/album.webp")

    assert first == second == (b"cover", "image/webp")
    request.get_res.assert_called_once_with("https://example.com/album.webp")
    response.close.assert_called_once()
    MediaChain._request_music_cover.cache_clear()


def test_recording_identity_rejects_multi_track_directory_scrape() -> None:
    """单曲身份不得覆盖整目录，否则会把同一首歌的标签写到专辑内所有文件。"""
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
    assert message == "单曲 MusicBrainz ID 仅支持刮削单个音频文件，整目录请选择专辑"


def test_default_scraping_config_enables_missing_only_music_lyrics() -> None:
    """新安装和未保存过该字段的用户应默认仅在缺失时下载歌词。"""
    assert ScrapingConfig.get_default_config()["music_lyrics"] == ScrapingPolicy.MISSINGONLY


def test_album_track_match_uses_disc_track_title_and_duration() -> None:
    """整张专辑刮削时应把本地音轨绑定到对应 Recording，不能复用专辑级身份。"""
    album = MusicAlbumInfo(
        source="musicbrainz",
        media_id="album-1",
        title="叶惠美",
        tracks=[
            MusicInfo(
                media_id="recording-1",
                title="以父之名",
                artists=["周杰伦"],
                disc_number=1,
                track_number=1,
                duration=342,
            ),
            MusicInfo(
                media_id="recording-3",
                title="晴天",
                artists=["周杰伦"],
                disc_number=1,
                track_number=3,
                duration=269,
            ),
        ],
    )

    matched = MediaChain._match_music_album_track(
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
    """标签和封面关闭时，歌词开关仍应独立驱动逐曲处理。"""
    chain = _media_chain()
    chain.storagechain = Mock()
    chain.scraping_policies = Mock()

    def scraping_option(_target, metadata):
        """仅开启歌词的缺失刮削策略。"""
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

    with patch("app.chain.music.MusicChain", return_value=music_chain):
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
    assert message == "已刮削 1 个音频文件，歌词新增 1 首、已存在 0 首、未匹配 0 首"
    call = chain._scrape_music_file.call_args
    assert call.kwargs["write_tags"] is False
    assert call.kwargs["with_cover"] is False
    assert call.kwargs["music_chain"] is music_chain


def test_write_music_lyrics_sidecar_creates_same_name_lrc(tmp_path) -> None:
    """同步歌词应以 UTF-8 同名 LRC 文件写入音轨所在目录。"""
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
    """仅缺失策略发现同名歌词后不得再次请求歌词源。"""
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
        music_chain=music_chain,
        album_info=None,
    )

    assert status == "existing"
    music_chain.lyrics.assert_not_called()


def test_write_music_lyrics_sidecar_uploads_to_remote_audio_directory(tmp_path) -> None:
    """远端存储歌词应使用音轨父目录和同名文件上传。"""
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
    """标签覆盖策略不得升级为全局覆盖并误覆盖缺失模式下的歌词。"""
    chain = _media_chain()
    chain.scrape_music_metadata = Mock(return_value=(True, "done"))
    fileitem = FileItem(storage="local", path="/music/叶惠美", type="dir")
    mediainfo = MusicInfo(
        source="musicbrainz",
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


def test_music_download_failure_is_attributed_only_to_enabled_outputs() -> None:
    """音频下载失败时只标记实际启用的标签、封面或歌词任务。"""
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
        music_chain=Mock(),
    )
    metadata_only = chain._scrape_music_file(
        fileitem=fileitem,
        mediainfo=MusicInfo(title="晴天"),
        write_tags=True,
        tag_overwrite=False,
        with_cover=False,
        lyrics_option=SimpleNamespace(is_skip=True),
        music_chain=None,
    )

    assert lyrics_only.metadata_success is True
    assert lyrics_only.lyrics_status == "failed"
    assert metadata_only.metadata_success is False
    assert metadata_only.lyrics_status == "disabled"
