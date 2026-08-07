from pathlib import Path
from types import SimpleNamespace

from app.chain.music import MusicChain
from app.chain.transfer import JobManager, TransferChain
from app.core.config import settings
from app.core.music import MusicInfo, MusicMeta
from app.helper.message import TemplateHelper
from app.schemas.file import FileItem
from app.schemas.transfer import TransferTask
from app.schemas.types import MediaType


def _music_context() -> tuple[MusicMeta, MusicInfo]:
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
