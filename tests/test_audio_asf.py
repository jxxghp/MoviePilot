"""真实ASF/WMA原生字段、旧编号、流声明和离线整理回归。"""

import hashlib
import os
import shutil
from pathlib import Path

import pytest
from mutagen.asf import ASF, ASFBoolAttribute, ASFByteArrayAttribute, ASFDWordAttribute

from app.application.audio import AudioMetadataHelper
from app.chain.transfer.music import _local_music_context, prepare_music_batch_context
from app.domain.context import MusicInfo
from app.schemas.types import MediaType
from tests.test_music_source_protection import _COVER, _GROUP, _RECORDING, _RELEASE, _TRACK
from tests.test_transfer_sync_extra_files import make_fileitem, make_transfer_chain


@pytest.fixture
def wma_path(tmp_path):
    """复制无标签的真实三秒WMAv2音频，测试不需要编码器。"""
    path = tmp_path / "03.wma"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.wma", path)
    return path


def _native_tags(path):
    """独立按Windows Media/Picard原生字段构造输入，不借助被测写入器。"""
    audio = ASF(path)
    audio.tags.update({
        "Title": ["晴天"], "Author": ["周杰伦"], "WM/AlbumTitle": ["叶惠美"], "WM/AlbumArtist": ["周杰伦"],
        "WM/Year": ["2020-07-31"], "WM/OriginalReleaseTime": ["2003-07-31"],
        "WM/TrackNumber": [ASFDWordAttribute(3)], "WM/PartOfSet": ["2/2"], "TOTALTRACKS": ["11"],
        "WM/ISRC": ["USQX91300105"], "WM/IsCompilation": [ASFBoolAttribute(True)],
        "MusicBrainz/Album Type": ["ep"], "MusicBrainz/Track Id": [_RECORDING],
        "MusicBrainz/Album Id": [_RELEASE], "MusicBrainz/Release Group Id": [_GROUP],
        "MusicBrainz/Release Track Id": [_TRACK], "WM/Composer": ["周杰伦"], "WM/Lyrics": ["歌词正文"],
    })
    audio.save()


def test_native_asf_tags_reach_offline_batch_and_keep_distinct_identities(wma_path):
    """标准标签贯通本地批次，不需要在线识别；发行和录音身份保持区分。"""
    _native_tags(wma_path)
    original = hashlib.sha256(wma_path.read_bytes()).digest()
    meta = AudioMetadataHelper.read_tags(wma_path)
    assert (meta.title, meta.artists, meta.album, meta.album_artist) == ("晴天", ["周杰伦"], "叶惠美", "周杰伦")
    assert (meta.year, meta.release_year, meta.original_year) == (2020, 2020, 2003)
    assert (meta.track_number, meta.total_tracks, meta.disc_number, meta.total_discs) == (3, 11, 2, 2)
    assert (meta.media_id, meta.musicbrainz_release_id, meta.musicbrainz_release_group_id, meta.musicbrainz_release_track_id) == (
        _RECORDING, _RELEASE, _GROUP, _TRACK)
    assert meta.album_type == "EP" and meta.secondary_types == ["Compilation"]
    assert meta.composers == ["周杰伦"] and meta.field_sources["composers"] == "tag"
    assert AudioMetadataHelper.read_lyrics(wma_path).plain_lyrics == "歌词正文"
    owner = make_transfer_chain()
    item = make_fileitem(str(wma_path))
    batch = prepare_music_batch_context(owner, [(item, False)], MediaType.MUSIC)
    _, info = _local_music_context(owner, item, wma_path, batch, AudioMetadataHelper.read(wma_path))
    assert info.title == "晴天" and info.album == "叶惠美" and info.composers == ["周杰伦"]
    assert hashlib.sha256(wma_path.read_bytes()).digest() == original


@pytest.mark.parametrize("value,expected", [("0", 1), (ASFDWordAttribute(2), 3), ("-1", None), ("bad", None)])
def test_asf_legacy_track_is_zero_based_and_never_negative(wma_path, value, expected):
    """旧WM/Track接受字符串与DWORD，零是第一轨；坏值不能变成有效曲序。"""
    audio = ASF(wma_path)
    audio.tags["WM/Track"] = [value]
    audio.save()
    assert AudioMetadataHelper.read_tags(wma_path).track_number == expected


def test_asf_standard_fields_override_old_aliases_and_correction_removes_stale_values(wma_path):
    """原生字段优先于旧版小写字段，纠正时只清理同字段别名而保留未知用户数据。"""
    _native_tags(wma_path)
    audio = ASF(wma_path)
    audio.tags.update({"artist": ["Old Artist"], "album": ["Old Album"], "WM/Track": [0],
                       "orchestra": ["Old Orchestra"], "WM/Ensemble": ["Old Orchestra"],
                       "performer:violin": ["Old Soloist"], "WM/Producer": ["Producer"], "USER/Binary": [b"\x01\x02"]})
    audio.save()
    meta = AudioMetadataHelper.read_tags(wma_path)
    assert meta.artists == ["周杰伦"] and meta.album == "叶惠美" and meta.track_number == 3
    meta.artists, meta.album, meta.track_number = ["New Artist"], "New Album", 4
    meta.orchestras, meta.performers = ["New Orchestra"], {"violin": ["New Soloist"]}
    assert AudioMetadataHelper.write(wma_path, meta)
    native = ASF(wma_path)
    assert [str(value) for value in native.tags["Author"]] == ["New Artist"]
    assert [str(value) for value in native.tags["WM/TrackNumber"]] == ["4/11"]
    assert not any(key in native.tags for key in ("artist", "album", "WM/Track", "orchestra", "WM/Ensemble", "performer:violin"))
    assert str(native.tags["WM/Producer"][0]) == "Producer" and native.tags["USER/Binary"][0].value == b"\x01\x02"
    actual = AudioMetadataHelper.read_tags(wma_path)
    assert actual.orchestras == ["New Orchestra"] and actual.performers == {"violin": ["New Soloist"]}


def test_asf_empty_and_binary_fields_do_not_fake_identity_or_tag_completeness(wma_path):
    """无标签音频保留流信息；错误类型属性及只有发行曲目ID时不能制造录音身份。"""
    meta = AudioMetadataHelper.read_tags(wma_path)
    assert meta.duration == 3 and meta.sample_rate == 44100 and meta.bitrate == 128000
    assert meta.audio_format == "WMA" and meta.audio_lossless is False
    assert meta.field_sources["audio_format"] == meta.field_sources["audio_lossless"] == "stream"
    assert meta.title is None and meta.artists == []
    audio = ASF(wma_path)
    audio.tags["WM/AlbumTitle"] = [ASFByteArrayAttribute(b"not text")]
    audio.tags["MusicBrainz/Track Id"] = [ASFByteArrayAttribute(_RECORDING.encode())]
    audio.tags["MusicBrainz/Release Track Id"] = [_TRACK]
    audio.save()
    meta = AudioMetadataHelper.read_tags(wma_path)
    assert meta.album is None and meta.media_id is None and meta.musicbrainz_release_track_id == _TRACK


@pytest.mark.parametrize("overwrite", [False, True])
def test_asf_identical_tags_and_preserved_cover_keep_hardlink(wma_path, overwrite):
    """完整年份精度与独立总曲数保持原样，缺失封面策略不触发无谓大文件复制。"""
    _native_tags(wma_path)
    audio = ASF(wma_path)
    audio.tags["WM/Picture"] = [ASFByteArrayAttribute(b"existing-picture")]
    audio.save()
    meta = AudioMetadataHelper.read_tags(wma_path)
    target = wma_path.with_name("library.wma")
    os.link(wma_path, target)
    before = target.read_bytes()
    assert AudioMetadataHelper.write(target, meta, overwrite=overwrite, cover_data=_COVER, cover_overwrite=False)
    assert os.path.samefile(wma_path, target) and target.read_bytes() == before


@pytest.mark.parametrize("codec,expected_format,lossless", [
    ("Windows Media Audio 9 Standard", "WMA", False),
    ("Windows Media Audio 9 Professional", "WMA", False),
    ("Windows Media Audio 9 Lossless", "WMA", True),
    ("", None, None),
])
def test_asf_quality_uses_codec_identity_not_filename_or_bitrate(wma_path, monkeypatch, codec, expected_format, lossless):
    """真实容器读取路径搭配受控Codec List投影；不声称静音WMAv2载荷变成了无损编码。"""
    audio = ASF(wma_path)
    audio.info.codec_type = codec
    audio.info.codec_name = "Untrusted Lossless Label"
    audio.info.codec_description = "Lossless 24 bit marketing text"
    monkeypatch.setattr(AudioMetadataHelper, "_open_audio", lambda _path: audio)
    meta = AudioMetadataHelper.read_tags(wma_path.with_suffix(".flac"))
    assert meta.audio_format == expected_format and meta.audio_lossless is lossless
    assert meta.bit_depth is None  # ASFInfo未提供实际位深，不能从描述伪造。


def test_asf_failed_cover_write_preserves_both_library_and_seed(wma_path, monkeypatch):
    """标签已写入临时副本但封面阶段失败时，原库目标和种子仍是原来的字节。"""
    target = wma_path.with_name("library.wma")
    os.link(wma_path, target)
    before = target.read_bytes()

    def fail_cover(**_kwargs):
        """在外部封面写入边界模拟磁盘错误，保留前半段真实ASF写入。"""
        raise OSError("cover failed")

    monkeypatch.setattr(AudioMetadataHelper, "_write_cover", fail_cover)
    assert not AudioMetadataHelper.write(target, MusicInfo(title="New"), cover_data=_COVER)
    assert os.path.samefile(wma_path, target) and target.read_bytes() == before
    assert not list(target.parent.glob("*.mp-audio-partial"))
