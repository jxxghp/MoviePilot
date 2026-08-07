from pathlib import Path
from types import SimpleNamespace

from app.helper.audio import AudioMetadataHelper


def test_read_audio_metadata_maps_easy_tags(monkeypatch):
    """音频标签和技术参数应映射为 MusicMeta。"""
    audio = SimpleNamespace(
        tags={
            "title": ["Get Lucky"],
            "artist": ["Daft Punk", "Pharrell Williams"],
            "album": ["Random Access Memories"],
            "albumartist": ["Daft Punk"],
            "date": ["2013-05-17"],
            "tracknumber": ["8/13"],
            "discnumber": ["1/1"],
            "isrc": ["USQX91300105"],
        },
        info=SimpleNamespace(
            length=369.4,
            bitrate=1411200,
            bits_per_sample=16,
            sample_rate=44100,
        ),
    )
    monkeypatch.setattr("app.helper.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    meta = AudioMetadataHelper.read(Path("/music/08 - Get Lucky.flac"))

    assert meta.title == "Get Lucky"
    assert meta.artists == ["Daft Punk", "Pharrell Williams"]
    assert meta.album == "Random Access Memories"
    assert meta.year == 2013
    assert meta.track_number == 8
    assert meta.total_tracks == 13
    assert meta.duration == 369
    assert meta.audio_format == "FLAC"


def test_read_audio_metadata_falls_back_to_filename(monkeypatch):
    """无法读取标签时应保留可用于手动整理的文件名元数据。"""
    monkeypatch.setattr("app.helper.audio.MutagenFile", lambda *_args, **_kwargs: None)

    meta = AudioMetadataHelper.read(Path("/music/Unknown Track.mp3"))

    assert meta.title == "Unknown Track"
    assert meta.audio_format == "MP3"
