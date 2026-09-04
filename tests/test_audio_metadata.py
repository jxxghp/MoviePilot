from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from mutagen.apev2 import APEBinaryValue
from mutagen.monkeysaudio import MonkeysAudio

from app.chain.media import MediaChain
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import (
    audio_quality_score,
    audio_quality_tier,
    format_audio_quality,
    parse_audio_quality,
)
from app.application.audio import AudioMetadataHelper
from app.schemas.types import MUSIC_ENTITY_ALBUM


RECORDING_ID = "38035858-f990-4fbb-b3b2-f2f8b958eeba"


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
            "musicbrainz_trackid": [RECORDING_ID],
        },
        info=SimpleNamespace(
            length=369.4,
            bitrate=1411200,
            bits_per_sample=16,
            sample_rate=44100,
        ),
    )
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)

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
    assert meta.media_source == "musicbrainz"
    assert meta.media_id == RECORDING_ID


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


def test_music_info_from_meta_preserves_track_and_audio_evidence():
    """核心元数据转换应保留整理、刮削和通知依赖的曲序与实际音频参数。"""
    from app.domain.meta.metamusic import MetaMusic

    info = MusicInfo.from_meta(MetaMusic(
        title="Get Lucky",
        artists=["Daft Punk"],
        album="Random Access Memories",
        disc_number=1,
        track_number=8,
        total_tracks=13,
        audio_format="FLAC",
        bit_depth=24,
        sample_rate=96_000,
        bitrate=2_304_000,
        duration=369,
        isrc="USQX91300105",
    ))

    assert info.track_number == 8
    assert info.total_tracks == 13
    assert info.audio_format == "FLAC"
    assert info.audio_lossless is True
    assert info.bit_depth == 24
    assert info.sample_rate == 96_000
    assert info.bitrate == 2_304_000
    assert info.duration == 369
    assert info.isrc == "USQX91300105"


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
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: None)

    meta = AudioMetadataHelper.read(Path("/music/Unknown Track.mp3"))

    assert meta.title == "Unknown Track"
    assert meta.audio_format == "MP3"


def test_read_audio_tags_does_not_fill_from_filename(monkeypatch):
    """标签识别层应只使用标签证据，避免把文件名线索提前混入。"""
    audio = SimpleNamespace(
        tags={"title": ["Tagged Title"]},
        info=SimpleNamespace(length=180),
    )
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    meta = AudioMetadataHelper.read_tags(Path("/music/Daft Punk - Get Lucky 2013.flac"))

    assert meta.title == "Tagged Title"
    assert meta.artists == []
    assert meta.year is None


def test_read_audio_tags_ignores_invalid_musicbrainz_id(monkeypatch):
    """异常 MusicBrainz 标签不得进入 ID 详情路径。"""
    audio = SimpleNamespace(
        tags={
            "title": ["Tagged Title"],
            "musicbrainz_trackid": ["../../unexpected"],
        },
        info=SimpleNamespace(length=180),
    )
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    meta = AudioMetadataHelper.read_tags(Path("/music/track.flac"))

    assert meta.media_source is None
    assert meta.media_id is None


def test_read_audio_tags_accepts_conventional_apev2_names(monkeypatch):
    """APE 常见字段名应映射为标准专辑、曲序、碟号和年份。"""
    audio = SimpleNamespace(
        tags={
            "title": ["天下太平"],
            "artist": ["陈奕迅", "张学友"],
            "album": ["Solidays"],
            "album artist": ["陈奕迅"],
            "track": ["12/14"],
            "disc": ["1/2"],
            "year": ["2008"],
        },
        info=SimpleNamespace(length=252),
    )
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    meta = AudioMetadataHelper.read_tags(Path("/music/天下太平.ape"))

    assert meta.album_artist == "陈奕迅"
    assert meta.track_number == 12
    assert meta.total_tracks == 14
    assert meta.disc_number == 1
    assert meta.total_discs == 2
    assert meta.year == 2008


def test_remote_path_meta_parses_track_prefix_once(tmp_path):
    """远程或尚未落盘的音频路径应先剥离曲序，不能把 08 误识别成艺术家。"""
    audio_path = tmp_path / "Daft Punk - Random Access Memories (2013)" / "08 - Get Lucky.flac"

    music_meta = MediaChain.read_path_meta(audio_path)

    assert music_meta.title == "Get Lucky"
    assert music_meta.artists == ["Daft Punk"]
    assert music_meta.album == "Random Access Memories"
    assert music_meta.track_number == 8
    assert music_meta.audio_format == "FLAC"


def test_read_audio_metadata_fallback_uses_dynamic_filename_parser(tmp_path, monkeypatch):
    """标签不可读时应直接使用完整动态模式解析复杂音乐文件名。"""
    audio_path = tmp_path / (
        "S H E - S H E十七音乐会 2018 WEB-DL 1080P AVC AAC-FHDMv.flac"
    )
    audio_path.write_bytes(b"fake-flac")
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: None)

    meta = AudioMetadataHelper.read(audio_path)

    assert meta.artists == ["S.H.E"]
    assert meta.title == "S.H.E十七音乐会"
    assert meta.year == 2018
    assert meta.audio_format == "FLAC"


def test_read_audio_metadata_partial_tags_use_filename_for_missing_fields(
        tmp_path,
        monkeypatch,
):
    """真实标签优先，缺失的艺术家和年份由完整文件名解析补齐。"""
    audio_path = tmp_path / "Daft Punk - Get Lucky 2013 FLAC.flac"
    audio_path.write_bytes(b"fake-flac")
    audio = SimpleNamespace(
        tags={"title": ["Tagged Title"]},
        info=SimpleNamespace(
            length=369.4,
            bitrate=1411200,
            bits_per_sample=16,
            sample_rate=44100,
        ),
    )
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    meta = AudioMetadataHelper.read(audio_path)

    assert meta.title == "Tagged Title"
    assert meta.artists == ["Daft Punk"]
    assert meta.year == 2013
    assert meta.duration == 369
    assert meta.sample_rate == 44100


def test_read_audio_metadata_distinguishes_alac_inside_m4a(monkeypatch):
    """M4A 容器应依据实际流编码区分 ALAC 与 AAC，避免把无损音频降级。"""
    audio = SimpleNamespace(
        tags={"title": ["Lossless Track"]},
        info=SimpleNamespace(
            codec="alac",
            codec_description="Apple Lossless Audio Codec",
            length=180,
            bitrate=900000,
            bits_per_sample=24,
            sample_rate=96000,
        ),
    )
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    meta = AudioMetadataHelper.read(Path("/music/Lossless Track.m4a"))

    assert meta.audio_format == "ALAC"
    assert meta.audio_lossless is True
    assert meta.audio_quality == "hires"


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
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    success = AudioMetadataHelper.write(
        Path("/music/08 - Get Lucky.flac"),
        MusicInfo(
            media_source="musicbrainz",
            media_id=RECORDING_ID,
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
    assert audio.tags["musicbrainz_trackid"] == [RECORDING_ID]


def test_write_audio_metadata_does_not_write_album_id_as_recording_tag(monkeypatch):
    """MusicBrainz 专辑身份不得写入只接受 recording ID 的曲目标签。"""
    class FakeAudio:
        """记录专辑元数据写入结果。"""

        def __init__(self):
            """初始化空标签容器。"""
            self.tags = {}

        def __setitem__(self, key, value):
            """记录 Easy 标签赋值。"""
            self.tags[key] = value

        def save(self):
            """模拟 Mutagen 保存。"""

    audio = FakeAudio()
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    success = AudioMetadataHelper.write(
        Path("/music/Random Access Memories.flac"),
        MusicInfo(
            media_source="musicbrainz",
            media_id="release-group-1",
            music_type=MUSIC_ENTITY_ALBUM,
            title="Random Access Memories",
        ),
    )

    assert success is True
    assert "musicbrainz_trackid" not in audio.tags


def test_write_audio_metadata_can_embed_cover_without_rewriting_tags(monkeypatch):
    """音乐封面策略应能在标签策略关闭时独立执行。"""
    audio = SimpleNamespace(tags={"title": ["Original"]})
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)
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


def test_write_audio_metadata_embeds_apev2_front_cover(monkeypatch):
    """APE 封面应按 APEv2 约定写入文件名、空字节和图片数据。"""
    class FakeMonkeysAudio(MonkeysAudio):
        """记录 Monkey's Audio 封面写入结果。"""

        def __init__(self):
            self.tags = {}
            self.saved = False

        def save(self, *_args, **_kwargs):
            self.saved = True

    audio = FakeMonkeysAudio()
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    AudioMetadataHelper._write_cover(
        path=Path("/music/track.ape"),
        cover_data=b"jpeg-data",
        cover_mime="image/jpeg",
        overwrite=True,
    )

    cover = audio.tags["Cover Art (Front)"]
    assert isinstance(cover, APEBinaryValue)
    assert bytes(cover) == b"cover.jpg\x00jpeg-data"
    assert audio.saved is True


def test_write_audio_metadata_preserves_existing_apev2_cover(monkeypatch):
    """关闭覆盖时应保留已有 APEv2 正面封面。"""
    class FakeMonkeysAudio(MonkeysAudio):
        """记录 Monkey's Audio 封面覆盖行为。"""

        def __init__(self):
            self.tags = {
                "Cover Art (Front)": APEBinaryValue(b"old.jpg\x00old-data")
            }
            self.saved = False

        def save(self, *_args, **_kwargs):
            self.saved = True

    audio = FakeMonkeysAudio()
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: audio)

    AudioMetadataHelper._write_cover(
        path=Path("/music/track.ape"),
        cover_data=b"new-data",
        cover_mime="image/jpeg",
        overwrite=False,
    )

    assert bytes(audio.tags["Cover Art (Front)"]) == b"old.jpg\x00old-data"
    assert audio.saved is False
