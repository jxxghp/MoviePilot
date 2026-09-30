"""用真实音频与链接验证标签刮削不会改写PT源文件。"""

import base64
import hashlib
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mutagen import File as MutagenFile
from mutagen.apev2 import APEv2
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from app.application.audio import AudioMetadataHelper
from app.application.transfer.workflow import TransferPlanningInput
from app.chain.scraping import ScrapingChain
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.file import FileItem
from tests.test_audio_containers import _write_dsf, _write_wave

_COVER = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")
_RECORDING = "38035858-f990-4fbb-b3b2-f2f8b958eeba"
_RELEASE = "70b71113-b078-4f07-9d24-374482016a41"
_GROUP = "25aa1b18-5e85-43c2-94c4-4b07ad96268a"
_TRACK = "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"


def _audio_file(tmp_path, container):
    """构造真实静音容器，编码来源与读取回归共用，不需要网络或ffmpeg。"""
    path = tmp_path / f"seed.{container}"
    if container == "wav":
        return _write_wave(path)
    if container == "dsf":
        return _write_dsf(path)
    shutil.copyfile(Path(__file__).parent / f"fixtures/audio/silence.{container}", path)
    return path


@pytest.mark.parametrize("container", ["wav", "dsf", "flac", "mp3", "m4a"])
@pytest.mark.parametrize("link_type", ["hardlink", "symlink"])
@pytest.mark.parametrize("wrong_extension", [False, True])
def test_native_container_writes_metadata_cover_and_release_ids_without_touching_seed(
        tmp_path, container, link_type, wrong_extension):
    """真实容器及错误后缀都须可补写标签/封面，保留不同身份与年份且源字节不变。"""
    source = _audio_file(tmp_path, container)
    extension = ("mp3" if container == "wav" else "wav") if wrong_extension else container
    target = tmp_path / f"library.{extension}"
    os.link(source, target) if link_type == "hardlink" else target.symlink_to(source)
    before = source.read_bytes()
    info = MusicInfo(title="歌曲", artists=["歌手", "Guest"], album="专辑", album_artist="专辑艺人",
                     year=2003, original_year=2003, release_year=2020, track_number=8, total_tracks=13,
                     disc_number=2, total_discs=2, isrc="USQX91300105", media_source="musicbrainz", media_id=_RECORDING,
                     musicbrainz_release_id=_RELEASE, musicbrainz_release_group_id=_GROUP, musicbrainz_release_track_id=_TRACK)

    assert AudioMetadataHelper.write(target, info, cover_data=_COVER, cover_mime="image/png")

    assert source.read_bytes() == before
    assert not os.path.samefile(source, target) and not target.is_symlink()
    read_back = AudioMetadataHelper.read_tags(target)
    assert (read_back.title, read_back.artists, read_back.album, read_back.album_artist) == (info.title, info.artists, info.album, info.album_artist)
    assert (read_back.year, read_back.release_year, read_back.original_year) == (2020, 2020, 2003)
    assert (read_back.track_number, read_back.total_tracks, read_back.disc_number, read_back.total_discs) == (8, 13, 2, 2)
    assert (read_back.media_id, read_back.musicbrainz_release_id, read_back.musicbrainz_release_group_id, read_back.musicbrainz_release_track_id) == (
        _RECORDING, _RELEASE, _GROUP, _TRACK)
    assert read_back.isrc == "USQX91300105"
    with target.open("rb") as stream:
        native = MutagenFile(fileobj=stream, filename="audio")
    if isinstance(native, FLAC):
        assert native.pictures[0].data == _COVER
    elif isinstance(native, MP4):
        assert bytes(native.tags["covr"][0]) == _COVER
    else:
        assert isinstance(native.tags, ID3) and native.tags.getall("APIC")[0].data == _COVER


@pytest.mark.parametrize("link_type", ["hardlink", "symlink"])
def test_music_metadata_write_detaches_destination_from_seed(tmp_path, link_type):
    """目标补写曲名后源文件仍逐字节相同，硬/软链接均不得穿透修改源标签。"""
    source = tmp_path / "seed.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", source)
    tags = FLAC(source)
    tags["title"] = ["Original title"]
    tags.save()
    target = tmp_path / "library.flac"
    os.link(source, target) if link_type == "hardlink" else target.symlink_to(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    assert AudioMetadataHelper.write(target, MusicInfo(title="Organized title"))

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert FLAC(source)["title"] == ["Original title"]
    assert FLAC(target)["title"] == ["Organized title"]
    assert not target.is_symlink() and not os.path.samefile(source, target)


@pytest.mark.parametrize("link_type", ["hardlink", "symlink"])
def test_failed_cover_write_keeps_seed_and_original_target(tmp_path, monkeypatch, link_type):
    """即使曲名已写入副本，后续封面失败也不能提交部分结果或改写源文件。"""
    source = tmp_path / "seed.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", source)
    target = tmp_path / "library.flac"
    os.link(source, target) if link_type == "hardlink" else target.symlink_to(source)
    before = source.read_bytes()
    monkeypatch.setattr(AudioMetadataHelper, "_write_cover", Mock(side_effect=OSError("磁盘写入失败")))

    assert not AudioMetadataHelper.write(target, MusicInfo(title="New"), cover_data=b"cover")

    assert source.read_bytes() == before and target.read_bytes() == before
    assert os.path.samefile(source, target)
    assert not list(tmp_path.glob(".*.mp-audio-partial"))


def test_disabled_metadata_outputs_do_not_detach_hardlink(tmp_path):
    """全部输出关闭时无需复制音频，保留原有链接及空间节约。"""
    source = tmp_path / "seed.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", source)
    target = tmp_path / "library.flac"
    os.link(source, target)

    assert AudioMetadataHelper.write(target, MusicInfo(title="New"), write_tags=False)
    assert os.path.samefile(source, target)


@pytest.mark.parametrize("container", ["wav", "dsf", "flac", "mp3", "m4a"])
@pytest.mark.parametrize("overwrite", [True, False])
@pytest.mark.parametrize("with_cover", [False, True])
def test_unchanged_metadata_keeps_existing_link(tmp_path, monkeypatch, container, overwrite, with_cover):
    """没有实际标签变化时保留硬链接，不能无故永久复制整张专辑占用空间。"""
    source = _audio_file(tmp_path, container)
    info = MusicInfo(title="Existing", artists=["Artist"], album="Album", track_number=1, disc_number=1)
    assert AudioMetadataHelper.write(source, info, cover_data=_COVER if with_cover else None)
    target = tmp_path / f"library.{container}"
    os.link(source, target)
    monkeypatch.setattr("app.application.audio.shutil.copy2", Mock(side_effect=AssertionError("完整标签不应复制音频")))

    assert AudioMetadataHelper.write(target, info, overwrite=overwrite, cover_data=_COVER if with_cover else None, cover_overwrite=False)

    assert os.path.samefile(source, target)
    assert not list(tmp_path.glob(".*.mp-audio-partial"))


def test_read_only_seed_permissions_are_preserved(tmp_path):
    """PT源只读也能补写独立目标，只读权限和源文件内容均保持原样。"""
    source = _audio_file(tmp_path, "flac")
    source.chmod(0o444)
    target = tmp_path / "library.flac"
    os.link(source, target)
    before = source.read_bytes()

    assert AudioMetadataHelper.write(target, MusicInfo(title="New"))

    assert source.read_bytes() == before and source.stat().st_mode & 0o777 == 0o444
    assert FLAC(target)["title"] == ["New"]


def test_concurrent_destination_change_cancels_replacement(tmp_path, monkeypatch):
    """目标被另一整理过程替换时不提交过时副本，也不覆盖另一过程的新内容。"""
    source = _audio_file(tmp_path, "flac")
    target = tmp_path / "library.flac"
    os.link(source, target)
    before = source.read_bytes()

    def change_destination(**_kwargs):
        """在临时副本写完标签后模拟另一个进程更新目录项。"""
        target.unlink()
        target.write_bytes(b"concurrent")

    monkeypatch.setattr(AudioMetadataHelper, "_write_cover", change_destination)

    assert not AudioMetadataHelper.write(target, MusicInfo(title="New"), cover_data=_COVER)

    assert source.read_bytes() == before and target.read_bytes() == b"concurrent"
    assert not list(tmp_path.glob(".*.mp-audio-partial"))


@pytest.mark.parametrize("link_type", ["hardlink", "symlink"])
def test_lyricsfile_overwrite_keeps_seed_sidecar(tmp_path, link_type):
    """Lyricsfile覆盖也须替换目标目录项，不能穿透从下载目录复制的链接。"""
    source = tmp_path / "seed.lyricsfile.yaml"
    source.write_text("original", encoding="utf-8")
    audio_path = tmp_path / "library.flac"
    target = audio_path.with_suffix(".lyricsfile.yaml")
    os.link(source, target) if link_type == "hardlink" else target.symlink_to(source)
    chain = object.__new__(ScrapingChain)

    assert chain._write_music_lyricsfile_sidecar(
        FileItem(storage="local", path=str(audio_path), name=audio_path.name, type="file"), audio_path, "updated")

    assert source.read_text(encoding="utf-8") == "original"
    assert target.read_text(encoding="utf-8") == "updated\n"


@pytest.mark.parametrize("model", [MusicInfo, MetaMusic])
@pytest.mark.parametrize("display_year", [2003, "2003"])
def test_original_year_does_not_become_current_release_date(tmp_path, model, display_year):
    """只确定首发年份的专辑不能制造当前发行年份，标签仍能保留和展示首发年。"""
    path = _audio_file(tmp_path, "flac")
    assert AudioMetadataHelper.write(path, model(title="Album", music_type="album", year=display_year, original_year=2003))
    tags = FLAC(path)
    assert tags["originaldate"] == ["2003"] and "date" not in tags
    read_back = AudioMetadataHelper.read_tags(path)
    assert (read_back.year, read_back.original_year, read_back.release_year) == (2003, 2003, None)


@pytest.mark.parametrize("mode", ["copy", "link", "softlink"])
def test_real_transfer_followed_by_scrape_preserves_seed_and_updates_library(tmp_path, monkeypatch, mode):
    """真实规划、存储执行及刮削入口串联后，补写只作用于最终命名的媒体库文件。"""
    source = _audio_file(tmp_path, "flac")
    before = source.read_bytes()
    storage = LocalStorage()
    source_item = storage.get_item(source)
    meta = MetaMusic(title="Song", album="Album", artists=["Artist"], album_artist="Artist", year=2004, track_number=1)
    info = MusicInfo.from_meta(meta)
    info.cover_url = "https://example.invalid/cover.png"
    planning_input = TransferPlanningInput(source_fileitem=source_item.model_dump(mode="json"), target_storage="local",
                                           target_path=str(tmp_path / "library"), requested_transfer_type=mode, need_rename=True)
    monkeypatch.setattr("app.modules.filemanager.transhandler.eventmanager.send_event", Mock(return_value=None))
    handler = TransHandler()
    checkpoint = handler.plan_transfer(planning_input, meta=meta, mediainfo=info, source_oper=storage,
                                       target_storage="local", target_path=tmp_path / "library", transfer_type=mode,
                                       need_scrape=True, need_rename=True, need_notify=False, overwrite_mode="never",
                                       episodes_info=None, preview=False)
    result = handler.execute_transfer_plan(checkpoint, meta=meta, mediainfo=info, source_oper=storage, target_oper=storage)
    assert result.success, result.message
    target = Path(checkpoint.final_target_path)
    assert target.name == "01 - Song.flac"
    if mode != "copy":
        assert source.samefile(target)

    chain = object.__new__(ScrapingChain)
    chain.storagechain = SimpleNamespace(download_file=storage.download)
    chain.scraping_policies = SimpleNamespace(option=lambda _target, kind: SimpleNamespace(is_skip=kind == "lyrics", is_overwrite=True))
    monkeypatch.setattr(chain, "_download_music_cover", lambda _url: (_COVER, "image/png"))

    success, message = chain.scrape_music_metadata(storage.get_item(target), mediainfo=info)

    assert success, message
    assert source.read_bytes() == before
    assert not source.samefile(target) and not target.is_symlink()
    tags = FLAC(target)
    assert (tags["title"], tags["album"], tags["artist"]) == (["Song"], ["Album"], ["Artist"])
    assert tags.pictures[0].data == _COVER


def test_native_ape_tags_keep_standard_names_and_identity_namespaces(tmp_path):
    """真实APEv2标签块保存后可按播放器通用键读回；此例不宣称验证APE音频解码。"""
    class ApeTagFile:
        """把原生APEv2标签块交给通用音频写入逻辑，不伪造标签映射行为。"""

        def __init__(self):
            """创建尚未附着音频的真实APEv2标签块。"""
            self.tags = APEv2()

        def __setitem__(self, key, value):
            """由Mutagen负责原生字段编码和校验。"""
            self.tags[key] = value

        def save(self, path):
            """写入实际APEv2二进制结构以验证读写回环。"""
            self.tags.save(path)

    path = tmp_path / "native-ape-tags"
    path.touch()
    info = MusicInfo(title="Song", album_artist="Artist", year=2020, track_number=2, total_tracks=8,
                     media_source="musicbrainz", media_id=_RECORDING, musicbrainz_release_id=_RELEASE,
                     musicbrainz_release_group_id=_GROUP, musicbrainz_release_track_id=_TRACK)
    AudioMetadataHelper._write_tag_values(ApeTagFile(), info, True, path)
    tags = APEv2(path)
    assert str(tags["Album Artist"]) == "Artist" and str(tags["Track"]) == "2/8"
    readable = AudioMetadataHelper._readable_tags(tags)
    assert AudioMetadataHelper._values(readable, "musicbrainz_track_id") == [_RECORDING]
    assert AudioMetadataHelper._values(readable, "musicbrainz_album_id") == [_RELEASE]
    assert AudioMetadataHelper._values(readable, "musicbrainz_release_group_id") == [_GROUP]
    assert AudioMetadataHelper._values(readable, "musicbrainz_release_track_id") == [_TRACK]
