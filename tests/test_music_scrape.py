from types import SimpleNamespace
from unittest.mock import Mock

from app.chain.media import MediaChain
from app.core.context import MUSIC_ENTITY_ALBUM, MusicInfo
from app.core.meta import MetaMusic
from app.schemas import FileItem


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
    chain.scraping_policies.option.return_value = SimpleNamespace(
        is_skip=False,
        is_overwrite=False,
    )
    audio_files = [
        FileItem(storage="local", path="/music/叶惠美/01.flac", type="file", name="01.flac"),
        FileItem(storage="local", path="/music/叶惠美/02.m4a", type="file", name="02.m4a"),
    ]
    chain.storagechain.list_files.return_value = audio_files
    chain._download_music_cover = Mock(return_value=(b"cover", "image/jpeg"))
    chain._scrape_music_file = Mock(return_value=True)
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
