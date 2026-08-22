from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.runtime.config import settings
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.adapters.media.audio import AudioMetadataHelper
from app.application.directory import DirectoryHelper
from app.runtime.hostports.directories import directory_config_port
from app.modules.medialibrary import MediaLibraryModule
from app.application.transferhandler import TransHandler
from app.schemas import FileItem, TransferDirectoryConf
from app.schemas.types import MediaType


def _audio_file(name: str, extension: str = "flac") -> FileItem:
    """构造标准专辑目录下的音频文件项。"""
    path = Path("/library/Daft Punk/Random Access Memories (2013)") / name
    return FileItem(
        storage="local",
        path=path.as_posix(),
        name=path.name,
        basename=path.stem,
        type="file",
        extension=extension,
    )


def _recording(**overrides) -> MusicInfo:
    """构造本地媒体库查重使用的单曲身份。"""
    values = {
        "media_source": "musicbrainz",
        "media_id": "recording-1",
        "title": "Get Lucky",
        "artists": ["Daft Punk"],
        "album": "Random Access Memories",
        "album_artist": "Daft Punk",
        "year": 2013,
        "disc_number": 1,
        "track_number": 8,
    }
    values.update(overrides)
    return MusicInfo(**values)


def test_music_media_root_uses_album_directory_with_or_without_disc_folder():
    """音乐根目录应稳定落在专辑层，不受动态 Disc 子目录是否渲染影响。"""
    album_dir = Path("/library/Daft Punk/Random Access Memories (2013)")

    assert DirectoryHelper.get_media_root_path(
        settings.MUSIC_RENAME_FORMAT,
        album_dir / "08 - Get Lucky.flac",
        media_type=MediaType.MUSIC,
    ) == album_dir
    assert DirectoryHelper.get_media_root_path(
        settings.MUSIC_RENAME_FORMAT,
        album_dir / "Disc 1" / "08 - Get Lucky.flac",
        media_type=MediaType.MUSIC,
    ) == album_dir


def test_media_files_uses_music_template_root_and_only_returns_audio():
    """本地音乐查重应扫描目标专辑目录并排除同目录视频文件。"""
    module = MediaLibraryModule()
    storage = Mock()
    album_dir = Path("/library/Daft Punk/Random Access Memories (2013)")
    storage.get_item.return_value = FileItem(
        storage="local",
        path=album_dir.as_posix(),
        name=album_dir.name,
        type="dir",
    )
    audio = _audio_file("08 - Get Lucky.flac")
    video = FileItem(
        storage="local",
        path=(album_dir / "Get Lucky.mkv").as_posix(),
        name="Get Lucky.mkv",
        basename="Get Lucky",
        type="file",
        extension="mkv",
    )
    module._MediaLibraryModule__get_storage_oper = Mock(return_value=storage)
    storage.list.return_value = [audio, video]
    directory = TransferDirectoryConf(
        library_path="/library",
        library_storage="local",
    )

    with patch.object(
        directory_config_port,
        "resolve",
        return_value=SimpleNamespace(get_library_dirs=lambda: [directory]),
    ), patch(
        "app.application.transferhandler.eventmanager.send_event",
        return_value=None,
    ):
        files = module.media_files(_recording())

    storage.get_item.assert_called_once_with(album_dir)
    assert files == [audio]


def test_local_music_recording_requires_matching_track_not_any_album_file():
    """单曲查重必须命中目标曲名，不能因同专辑存在其它音轨而误判完成。"""
    module = MediaLibraryModule()
    module.media_files = Mock(
        return_value=[
            _audio_file("01 - Give Life Back to Music.flac"),
            _audio_file("08 - Get Lucky.flac"),
        ]
    )

    with patch("app.modules.medialibrary.settings.LOCAL_EXISTS_SEARCH", True):
        exists = module.media_exists(_recording())
        missing = module.media_exists(
            _recording(title="Instant Crush", media_id="recording-2", track_number=5)
        )

    assert exists and exists.type == MediaType.MUSIC
    assert missing is None


def test_local_music_album_requires_unique_complete_track_coverage():
    """专辑查重应按去重曲目数判定完整，重复格式不能冒充缺失音轨。"""
    module = MediaLibraryModule()
    duplicate = _audio_file("01 - Give Life Back to Music.mp3", extension="mp3")
    files = [
        _audio_file("01 - Give Life Back to Music.flac"),
        duplicate,
        _audio_file("02 - The Game of Love.flac"),
    ]
    module.media_files = Mock(return_value=files)
    album = _recording(
        music_type="album",
        media_id="release-group-1",
        title="Random Access Memories",
        track_number=None,
        total_tracks=3,
    )

    with patch("app.modules.medialibrary.settings.LOCAL_EXISTS_SEARCH", True):
        assert module.media_exists(album) is None
        files.append(_audio_file("03 - Giorgio by Moroder.flac"))
        exists = module.media_exists(album)

    assert exists and exists.type == MediaType.MUSIC


def test_local_music_album_with_unknown_total_is_not_assumed_complete():
    """未知专辑总曲目数时不能仅凭目录中存在音频就判定订阅完成。"""
    assert MediaLibraryModule._music_album_is_complete(
        [_audio_file("01 - Intro.flac")],
        total_tracks=None,
    ) is False


def test_local_music_album_counts_same_track_number_on_different_discs():
    """多碟专辑中相同曲序应通过盘号区分为不同曲目。"""
    files = [
        _audio_file("Disc 1/01 - Intro.flac"),
        _audio_file("Disc 2/01 - Finale.flac"),
    ]

    assert MediaLibraryModule._music_album_is_complete(files, total_tracks=2) is True


def test_music_size_overwrite_prefers_actual_audio_quality(tmp_path, monkeypatch):
    """音乐的 size 覆盖判断应优先使用音频参数，而不是把字节数当作音质。"""
    target_path = tmp_path / "Track.flac"
    target_path.write_bytes(b"target")
    target_item = FileItem(
        storage="local",
        path=target_path.as_posix(),
        name=target_path.name,
        basename=target_path.stem,
        type="file",
        extension="flac",
    )
    music = _recording()

    monkeypatch.setattr(
        AudioMetadataHelper,
        "read",
        lambda path: MetaMusic(audio_format="MP3", bitrate=320000),
    )
    upgrade = TransHandler._TransHandler__music_quality_overwrite_decision(
        MetaMusic(audio_format="FLAC", bit_depth=24, sample_rate=192000),
        music,
        target_item,
    )

    monkeypatch.setattr(
        AudioMetadataHelper,
        "read",
        lambda path: MetaMusic(audio_format="FLAC", bit_depth=24, sample_rate=192000),
    )
    downgrade = TransHandler._TransHandler__music_quality_overwrite_decision(
        MetaMusic(audio_format="MP3", bitrate=320000),
        music,
        target_item,
    )
    non_music = TransHandler._TransHandler__music_quality_overwrite_decision(
        MetaMusic(audio_format="FLAC"),
        SimpleNamespace(type=MediaType.MOVIE),
        target_item,
    )

    assert upgrade is True
    assert downgrade is False
    assert non_music is None
