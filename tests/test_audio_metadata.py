from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.core.context import MusicInfo
from app.core.meta.metamusic import (
    audio_quality_score,
    audio_quality_tier,
    format_audio_quality,
    parse_audio_quality,
)
from app.helper.audio import AudioMetadataHelper


def test_read_audio_metadata_maps_easy_tags(monkeypatch):
    """音频标签和技术参数应映射为 MetaMusic。"""
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
    assert meta.audio_lossless is True
    assert meta.audio_quality == "lossless"
    assert meta.audio_specs == "FLAC · 16-bit · 44.1 kHz · 1,411 kbps"


def test_parse_declared_hires_audio_quality_from_resource_title():
    """站点资源标题中的格式、位深和采样率应形成可筛选的统一音质参数。"""
    specs = parse_audio_quality("周杰伦 - 叶惠美 FLAC 24bit 96kHz Hi-Res")

    assert specs == {
        "audio_format": "FLAC",
        "audio_lossless": True,
        "bit_depth": 24,
        "sample_rate": 96000,
        "bitrate": None,
    }
    assert audio_quality_tier(**specs) == "hires"
    assert audio_quality_score(**specs) == 96
    assert format_audio_quality(**specs) == "FLAC · 24-bit · 96 kHz"


def test_audio_quality_score_orders_lossy_lossless_and_terminal_hires():
    """音乐洗版分数必须稳定满足有损、无损、顶级 Hi-Res 的递增关系。"""
    mp3_score = audio_quality_score("MP3", bitrate=320000)
    flac_score = audio_quality_score("FLAC", bit_depth=16, sample_rate=44100)
    hires_score = audio_quality_score("FLAC", bit_depth=24, sample_rate=192000)

    assert 0 < mp3_score < flac_score < hires_score
    assert hires_score == 100


def test_music_info_serialization_exposes_derived_audio_quality():
    """音乐 REST 序列化应同时返回原始技术参数和规范化音质展示字段。"""
    payload = MusicInfo(
        title="晴天",
        audio_format="FLAC",
        bit_depth=24,
        sample_rate=96_000,
        bitrate=2_304_000,
    ).to_dict()

    assert payload["audio_quality"] == "hires"
    assert payload["audio_quality_score"] == 96
    assert payload["audio_specs"] == "FLAC · 24-bit · 96 kHz · 2,304 kbps"


def test_parse_compact_audio_quality_tokens_without_false_sample_bitrate():
    """紧凑资源命名中的 FLAC24bit 和 320K 应可识别，96kHz 不得误判为码率。"""
    lossless = parse_audio_quality("Album.FLAC24bit.96kHz")
    lossy = parse_audio_quality("Album.MP3.320K")

    assert lossless["audio_format"] == "FLAC"
    assert lossless["bit_depth"] == 24
    assert lossless["sample_rate"] == 96000
    assert lossless["bitrate"] is None
    assert lossy["bitrate"] == 320000


def test_read_audio_metadata_falls_back_to_filename(monkeypatch):
    """无法读取标签时应保留可用于手动整理的文件名元数据。"""
    monkeypatch.setattr("app.helper.audio.MutagenFile", lambda *_args, **_kwargs: None)

    meta = AudioMetadataHelper.read(Path("/music/Unknown Track.mp3"))

    assert meta.title == "Unknown Track"
    assert meta.audio_format == "MP3"


def test_write_audio_metadata_maps_music_info_to_easy_tags(monkeypatch):
    """音乐刮削应把标准歌曲、专辑和曲序字段写回音频标签。"""
    class FakeAudio:
        """记录 Mutagen Easy 标签写入结果。"""

        def __init__(self):
            self.tags = {}
            self.saved = False

        def __setitem__(self, key, value):
            self.tags[key] = value

        def save(self):
            self.saved = True

    audio = FakeAudio()
    monkeypatch.setattr("app.helper.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    success = AudioMetadataHelper.write(
        Path("/music/08 - Get Lucky.flac"),
        MusicInfo(
            title="Get Lucky",
            artists=["Daft Punk", "Pharrell Williams"],
            album="Random Access Memories",
            album_artist="Daft Punk",
            year=2013,
            track_number=8,
            total_tracks=13,
            isrc="USQX91300105",
        ),
    )

    assert success is True
    assert audio.saved is True
    assert audio.tags["title"] == ["Get Lucky"]
    assert audio.tags["artist"] == ["Daft Punk", "Pharrell Williams"]
    assert audio.tags["tracknumber"] == ["8/13"]


def test_write_audio_metadata_can_embed_cover_without_rewriting_tags(monkeypatch):
    """音乐封面策略应能在标签策略关闭时独立执行。"""
    audio = SimpleNamespace(tags={"title": ["Original"]})
    monkeypatch.setattr("app.helper.audio.MutagenFile", lambda *_args, **_kwargs: audio)
    write_cover = Mock()
    monkeypatch.setattr(AudioMetadataHelper, "_write_cover", write_cover)

    success = AudioMetadataHelper.write(
        Path("/music/track.flac"),
        MusicInfo(title="Changed"),
        cover_data=b"cover",
        write_tags=False,
        cover_overwrite=False,
    )

    assert success is True
    assert audio.tags == {"title": ["Original"]}
    write_cover.assert_called_once_with(
        path=Path("/music/track.flac"),
        cover_data=b"cover",
        cover_mime="image/jpeg",
        overwrite=False,
    )
