"""
通过真实音频容器覆盖空标签、原生 ID3 和独立音轨总数的读取契约。
"""

from tests.support.music.containers import (
    _write_aiff,
    _write_dff,
    _write_dsf,
    _write_wave,
)

import hashlib
import shutil
import struct
import wave
from pathlib import Path

import pytest
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC, TXXX, UFID
from mutagen.wave import WAVE

from app.application.audio import AudioMetadataHelper

RECORDING_ID = "38035858-f990-4fbb-b3b2-f2f8b958eeba"


@pytest.fixture
def flac_path(tmp_path):
    """
    复制无标签的三秒静音 FLAC，所有标签写入仅作用于测试临时文件。
    """
    path = tmp_path / "03.flac"
    shutil.copyfile(Path(__file__).parents[2] / "fixtures/audio/silence.flac", path)
    return path


@pytest.mark.parametrize("container", ["wav", "flac"])
def test_untagged_audio_keeps_stream_and_path_evidence(container, tmp_path, flac_path):
    """
    没有标签仍能读取时长和音质，并用路径补齐曲序而不改动文件。
    """
    path = _write_wave(tmp_path / "03.wav") if container == "wav" else flac_path
    before = hashlib.sha256(path.read_bytes()).digest()

    merged, tags, filename = AudioMetadataHelper.read_evidence(path)

    assert tags is not None
    assert tags.title is None
    assert tags.duration == merged.duration == 3
    assert merged.sample_rate == 44100
    assert merged.bit_depth == 16
    assert merged.track_number == filename.track_number == 3
    assert hashlib.sha256(path.read_bytes()).digest() == before


@pytest.mark.parametrize("container", ["wav", "dsf", "aiff", "dff"])
def test_raw_id3_container_reads_track_identity(container, tmp_path):
    """
    WAV/DSF/AIFF/DSDIFF原生ID3帧能提供曲目字段及Recording身份。
    """
    path = tmp_path / f"03.{container}"
    factory = {"wav": _write_wave, "dsf": _write_dsf, "aiff": _write_aiff, "dff": _write_dff}
    audio = MutagenFile(factory[container](path))
    audio.add_tags()
    for frame in (
        TIT2(encoding=3, text=["晴天"]),
        TPE1(encoding=3, text=["周杰伦", "嘉宾"]),
        TALB(encoding=3, text=["叶惠美"]),
        TPE2(encoding=3, text=["周杰伦"]),
        TDRC(encoding=3, text=["2003-07-31"]),
        TRCK(encoding=3, text=["3/11"]),
        TPOS(encoding=3, text=["1/2"]),
        TSRC(encoding=3, text=["USQX91300105"]),
        TXXX(encoding=3, desc="VERSION", text=["Live"]),
        UFID(owner="http://musicbrainz.org", data=RECORDING_ID.encode("ascii")),
    ):
        audio.tags.add(frame)
    audio.save()
    before = path.read_bytes()

    meta = AudioMetadataHelper.read_tags(path)

    assert meta.title == "晴天"
    assert meta.artists == ["周杰伦", "嘉宾"]
    assert meta.album == "叶惠美"
    assert meta.album_artist == "周杰伦"
    assert meta.year == 2003
    assert (meta.track_number, meta.total_tracks) == (3, 11)
    assert (meta.disc_number, meta.total_discs) == (1, 2)
    assert meta.isrc == "USQX91300105"
    assert meta.version == "Live"
    assert meta.media_id == RECORDING_ID
    assert meta.media_source == "musicbrainz"
    assert path.read_bytes() == before


@pytest.mark.parametrize("track_total,disc_total", [
    ("TRACKTOTAL", "DISCTOTAL"), ("TOTALTRACKS", "TOTALDISCS"),
])
def test_flac_reads_separate_track_and_disc_totals(flac_path, track_total, disc_total):
    """
    Vorbis 的独立总数字段与组合编号拥有同等语义。
    """
    audio = FLAC(flac_path)
    audio.update({"title": ["晴天"], "tracknumber": ["3"], "discnumber": ["1"],
                  track_total: ["11"], disc_total: ["2"]})
    audio.save()

    meta = AudioMetadataHelper.read_tags(flac_path)

    assert (meta.track_number, meta.total_tracks) == (3, 11)
    assert (meta.disc_number, meta.total_discs) == (1, 2)


@pytest.mark.parametrize("position,expected", [("3/11", (3, 11)), ("3/0", (3, 12)),
                                                ("0/0", (None, 12)), ("bad", (None, 12))])
def test_combined_position_prefers_valid_total(flac_path, position, expected):
    """
    有效组合总数优先，零或坏编号不能污染真实的独立总数。
    """
    audio = FLAC(flac_path)
    audio.update({"tracknumber": [position], "tracktotal": ["12"]})
    audio.save()

    meta = AudioMetadataHelper.read_tags(flac_path)

    assert (meta.track_number, meta.total_tracks) == expected


def test_id3_release_track_id_is_not_recording_identity(tmp_path):
    """
    发行曲目 ID 不能被当作 Recording ID 传入指纹和单曲识别。
    """
    path = _write_wave(tmp_path / "03.wav")
    audio = WAVE(path)
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=["晴天"]))
    audio.tags.add(TXXX(encoding=3, desc="MusicBrainz Release Track Id", text=[RECORDING_ID]))
    audio.tags.add(UFID(owner="http://musicbrainz.org", data=b"\xffinvalid"))
    audio.save()

    meta = AudioMetadataHelper.read_tags(path)

    assert meta.title == "晴天"
    assert meta.media_id is None
    assert meta.media_source is None
