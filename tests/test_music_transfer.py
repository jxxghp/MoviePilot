from pathlib import Path
from types import SimpleNamespace

from jinja2 import Template

from app.chain.music import MusicChain
from app.chain.transfer import JobManager, TransferChain
from app.core.config import settings
from app.core.meta import MetaMusic
from app.core.context import MusicInfo
from app.helper.message import TemplateHelper
from app.schemas.file import FileItem
from app.schemas.transfer import TransferTask
from app.schemas.types import MediaType


def _music_context() -> tuple[MetaMusic, MusicInfo]:
    """构造整理测试使用的音乐元数据和媒体信息。"""
    info = MusicInfo(
        source="musicbrainz",
        media_id="recording-1",
        title="Get Lucky",
        artists=["Daft Punk", "Pharrell Williams"],
        album="Random Access Memories",
        album_artist="Daft Punk",
        year=2013,
        track_number=8,
        total_tracks=13,
        category="Album",
    )
    return MusicChain.to_meta(info), info


def test_music_rename_context_contains_audio_fields():
    """重命名模板上下文应提供艺术家、专辑、盘号和曲序字段。"""
    meta, info = _music_context()

    context = TemplateHelper().builder.build(
        meta=meta,
        mediainfo=info,
        file_extension=".flac",
        include_raw_objects=False,
    )

    assert context["artist"] == "Daft Punk ／ Pharrell Williams"
    assert context["album"] == "Random Access Memories"
    assert context["track"] == "08"
    assert context["fileExt"] == ".flac"


def test_music_rename_prefers_track_meta_over_album_media():
    """专辑整理时应使用每个文件的曲名和曲序，不能把专辑名写成所有目标文件名。"""
    meta = MetaMusic(
        org_string="10. 明天晴天.m4a",
        title="明天晴天",
        artists=["孙燕姿"],
        album="完美的一天",
        album_artist="孙燕姿",
        year=2005,
        track_number=10,
        total_tracks=11,
    )
    album = MusicInfo(
        source="musicbrainz",
        media_id="album-1",
        music_type="album",
        title="完美的一天",
        artists=["孙燕姿"],
        album="完美的一天",
        album_artist="孙燕姿",
        year=2005,
        total_tracks=11,
    )

    context = TemplateHelper().builder.build(
        meta=meta,
        mediainfo=album,
        file_extension=".m4a",
        include_raw_objects=False,
    )
    rendered = Template(settings.MUSIC_RENAME_FORMAT).render(context)

    assert context["title"] == "明天晴天"
    assert context["track"] == "10"
    assert rendered == "孙燕姿/完美的一天 (2005)/10 - 明天晴天.m4a"


def test_music_rename_format_is_independent_from_movie_format():
    """音乐应使用独立重命名模板且保持影视模板不变。"""
    assert settings.RENAME_FORMAT(MediaType.MUSIC) == settings.MUSIC_RENAME_FORMAT
    assert settings.RENAME_FORMAT(MediaType.MOVIE) == settings.MOVIE_RENAME_FORMAT


def test_audio_is_primary_only_in_music_context():
    """音频文件只在音乐上下文中作为主要媒体文件。"""
    chain = TransferChain()
    audio = FileItem(
        storage="local",
        path="/music/track.flac",
        name="track.flac",
        basename="track",
        type="file",
        extension="flac",
    )

    assert chain._is_primary_media_file(audio, MusicInfo(title="Track")) is True
    assert chain._is_primary_media_file(audio, None) is False


def test_restore_music_context_from_download_history():
    """自动整理应从下载历史备注恢复标准音乐身份。"""
    meta, info = _music_context()
    history = SimpleNamespace(
        note={
            "music": {
                "version": 1,
                "meta": meta.to_dict(),
                "media": info.to_dict(),
            }
        }
    )

    restored_meta, restored_info = TransferChain._restore_music_download_context(
        history,
        Path("/remote/08 - Get Lucky.flac"),
    )

    assert restored_meta is not None
    assert restored_info is not None
    assert restored_meta.org_string == "08 - Get Lucky.flac"
    assert restored_info.source == "musicbrainz"
    assert restored_info.media_id == "recording-1"
    assert restored_info.album == "Random Access Memories"


def test_restore_album_context_keeps_album_identity_and_track_specific_tags(tmp_path, monkeypatch):
    """整专整理应保留选中的专辑身份，同时使用每个文件自己的曲名、艺术家和曲序。"""
    album = MusicInfo(
        source="musicbrainz",
        media_id="release-group-1",
        music_type="album",
        title="叶惠美",
        artists=["周杰伦"],
        album="叶惠美",
        album_artist="周杰伦",
        year=2003,
        total_tracks=11,
    )
    meta = MusicChain.to_meta(album)
    history = SimpleNamespace(note={
        "music": {
            "version": 1,
            "meta": meta.to_dict(),
            "media": album.to_dict(),
        }
    })
    audio_file = tmp_path / "03. 晴天.flac"
    audio_file.write_bytes(b"fake-flac")

    from app.helper.audio import AudioMetadataHelper

    monkeypatch.setattr(
        AudioMetadataHelper,
        "read",
        lambda path: MetaMusic(
            org_string=path.name,
            title="晴天",
            artists=["周杰伦"],
            album="错误专辑",
            album_artist="错误艺术家",
            year=1999,
            track_number=3,
            total_tracks=99,
        ),
    )

    restored_meta, restored_info = TransferChain._restore_music_download_context(history, audio_file)

    assert restored_meta.title == "晴天"
    assert restored_meta.track_number == 3
    assert restored_meta.album == "叶惠美"
    assert restored_meta.album_artist == "周杰伦"
    assert restored_meta.year == 2003
    assert restored_meta.total_tracks == 11
    assert restored_info.music_type == "album"
    assert restored_info.media_id == "release-group-1"


def test_restore_music_context_uses_file_title_over_subscription_title(tmp_path, monkeypatch):
    """曲目标题应优先取当前文件自身的标签/文件名，而非沿用订阅时的单曲标题。"""
    meta, info = _music_context()
    history = SimpleNamespace(
        note={
            "music": {
                "version": 1,
                "meta": meta.to_dict(),
                "media": info.to_dict(),
            }
        }
    )
    # 订阅标题被识别为单曲"Get Lucky"，而实际音轨标签曲目名 不同且无身份字段
    audio_file = tmp_path / "07.幸福.flac"
    audio_file.write_bytes(b"fake-flac")

    from app.helper.audio import AudioMetadataHelper

    monkeypatch.setattr(
        AudioMetadataHelper,
        "read",
        lambda path: MetaMusic(org_string=path.name, title="流浪地图"),
    )

    restored_meta, _ = TransferChain._restore_music_download_context(history, audio_file)

    assert restored_meta is not None
    assert restored_meta.title == "流浪地图"


def test_restore_music_context_uses_filename_when_source_is_not_locally_accessible():
    """远端音频无法直接读取标签时也应按文件名区分曲目，避免整张专辑重名。"""
    meta, info = _music_context()
    meta.artists = ["孙燕姿"]
    meta.title = "完美的一天"
    meta.album = "完美的一天"
    meta.album_artist = "孙燕姿"
    meta.year = 2005
    meta.track_number = None
    meta.total_tracks = None
    info.music_type = "album"
    info.artists = ["孙燕姿"]
    info.title = "完美的一天"
    info.album = "完美的一天"
    info.album_artist = "孙燕姿"
    info.year = 2005
    info.track_number = None
    info.total_tracks = None
    history = SimpleNamespace(
        note={
            "music": {
                "version": 1,
                "meta": meta.to_dict(),
                "media": info.to_dict(),
            }
        }
    )

    restored_meta, restored_info = TransferChain._restore_music_download_context(
        history,
        Path("/remote/10. 明天晴天.m4a"),
    )

    assert restored_meta is not None
    assert restored_info is not None
    assert restored_meta.title == "10. 明天晴天"
    assert restored_info.title == "10. 明天晴天"
    context = TemplateHelper().builder.build(
        meta=restored_meta,
        mediainfo=restored_info,
        file_extension=".m4a",
        include_raw_objects=False,
    )
    rendered = Template(settings.MUSIC_RENAME_FORMAT).render(context)
    assert rendered == "孙燕姿/完美的一天 (2005)/10. 明天晴天.m4a"


def test_job_manager_serializes_music_queue_models():
    """整理队列应使用音乐专属 Schema 序列化任务。"""
    meta, info = _music_context()
    task = TransferTask(
        fileitem=FileItem(
            storage="local",
            path="/music/track.flac",
            name="track.flac",
            basename="track",
            type="file",
            extension="flac",
        ),
        meta=meta,
        mediainfo=info,
    )
    manager = JobManager()

    assert manager.add_task(task) is True
    job = manager.list_jobs()[0]
    assert job.media.type == "音乐"
    assert job.media.album == "Random Access Memories"
    assert job.tasks[0].meta.type == "音乐"
