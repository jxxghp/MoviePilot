"""通过真实音频容器覆盖空标签、原生 ID3 和独立音轨总数的读取契约。"""

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


def _write_wave(path: Path) -> Path:
    """生成三秒双声道 PCM，避免测试依赖本机 ffmpeg。"""
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(b"\0" * 44100 * 4 * 3)
    return path


def _write_dsf(path: Path) -> Path:
    """生成带单个双声道 DSD 数据块的合法 DSF，供实际 ID3 读写使用。"""
    payload = b"\x69" * 8192
    header = struct.pack("<4sQQQ", b"DSD ", 28, 92 + len(payload), 0)
    fmt = struct.pack("<4sQIIIIIIQII", b"fmt ", 52, 1, 0, 2, 2, 2822400, 1, 32768, 4096, 0)
    path.write_bytes(header + fmt + struct.pack("<4sQ", b"data", 12 + len(payload)) + payload)
    return path


def _write_aiff(path: Path) -> Path:
    """标准FORM/COMM/SSND块生成三秒44.1kHz PCM，采样率使用AIFF扩展浮点格式。"""
    common = struct.pack(">hIh", 2, 44100 * 3, 16) + bytes.fromhex("400eac44000000000000")
    sound = b"\0" * (8 + 44100 * 4 * 3)
    chunks = b"COMM" + struct.pack(">I", len(common)) + common + b"SSND" + struct.pack(">I", len(sound)) + sound
    path.write_bytes(b"FORM" + struct.pack(">I", len(chunks) + 4) + b"AIFF" + chunks)
    return path


def _write_dff(path: Path) -> Path:
    """生成合法DSDIFF声道交织DSD数据及PROP，验证与DSF不同的原生ID3容器。"""
    def chunk(name: bytes, data: bytes) -> bytes:
        """DSDIFF块以64位大端长度计数，奇数字节补齐不计入内容长度。"""
        return name + struct.pack(">Q", len(data)) + data + b"\0" * (len(data) % 2)

    properties = b"SND " + chunk(b"FS  ", struct.pack(">I", 2822400))
    properties += chunk(b"CHNL", struct.pack(">H", 2) + b"SLFTSRGT") + chunk(b"CMPR", b"DSD \x00")
    content = b"DSD " + chunk(b"FVER", struct.pack(">I", 0x01050000)) + chunk(b"PROP", properties)
    content += chunk(b"DSD ", b"\x69" * (2822400 * 3 * 2 // 8))
    path.write_bytes(chunk(b"FRM8", content))
    return path


@pytest.fixture
def flac_path(tmp_path):
    """复制无标签的三秒静音 FLAC，所有标签写入仅作用于测试临时文件。"""
    path = tmp_path / "03.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
    return path


@pytest.mark.parametrize("container", ["wav", "flac"])
def test_untagged_audio_keeps_stream_and_path_evidence(container, tmp_path, flac_path):
    """没有标签仍能读取时长和音质，并用路径补齐曲序而不改动文件。"""
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
    """WAV/DSF/AIFF/DSDIFF原生ID3帧能提供曲目字段及Recording身份。"""
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
    """Vorbis 的独立总数字段与组合编号拥有同等语义。"""
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
    """有效组合总数优先，零或坏编号不能污染真实的独立总数。"""
    audio = FLAC(flac_path)
    audio.update({"tracknumber": [position], "tracktotal": ["12"]})
    audio.save()

    meta = AudioMetadataHelper.read_tags(flac_path)

    assert (meta.track_number, meta.total_tracks) == expected


def test_id3_release_track_id_is_not_recording_identity(tmp_path):
    """发行曲目 ID 不能被当作 Recording ID 传入指纹和单曲识别。"""
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
