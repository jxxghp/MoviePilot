"""真实容器、来源响应及 JSON 快照分别保留录音与发行身份和字段来源。"""

import asyncio
import json
import pickle
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from mutagen import File
from mutagen.flac import FLAC
from mutagen.id3 import TALB, TDOR, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, UFID
from mutagen.mp4 import MP4

from app.application.audio import AudioMetadataHelper
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.transfer.facade import TransferChain
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.musicbrainz import MusicBrainzModule
from app.schemas.music import MusicInfo as MusicInfoSchema
from app.schemas.music import MusicMeta as MusicMetaSchema
from tests.test_audio_containers import _write_dsf, _write_wave
from tests.test_transfer_sync_extra_files import make_fileitem

RECORDING = "11111111-1111-4111-8111-111111111111"
RELEASE = "22222222-2222-4222-8222-222222222222"
GROUP = "33333333-3333-4333-8333-333333333333"
TRACK = "44444444-4444-4444-8444-444444444444"
IDENTITIES = {
    "musicbrainz_release_id": RELEASE,
    "musicbrainz_release_group_id": GROUP,
    "musicbrainz_release_track_id": TRACK,
}


def _tag_audio(path: Path) -> None:
    """按容器的实际规范写入不同层级 ID，不调用产品标签写入实现。"""
    if path.suffix == ".flac":
        audio = FLAC(path)
        audio.update({
            "title": ["Song"], "artist": ["Artist"], "albumartist": ["Album Artist"], "album": ["Record"],
            "date": ["2020-06-01"], "originaldate": ["2003-01-01"], "tracknumber": ["3/12"], "discnumber": ["2/2"],
            "musicbrainz_trackid": [RECORDING], "musicbrainz_albumid": [RELEASE],
            "musicbrainz_releasegroupid": [GROUP], "musicbrainz_releasetrackid": [TRACK],
        })
    elif path.suffix == ".m4a":
        audio = MP4(path)
        audio.update({"\xa9nam": ["Song"], "\xa9ART": ["Artist"], "\xa9alb": ["Record"],
                      "aART": ["Album Artist"], "\xa9day": ["2020-06-01"], "trkn": [(3, 12)], "disk": [(2, 2)]})
        for name, value in (("MusicBrainz Track Id", RECORDING), ("MusicBrainz Album Id", RELEASE),
                            ("MusicBrainz Release Group Id", GROUP), ("MusicBrainz Release Track Id", TRACK),
                            ("ORIGINALDATE", "2003-01-01")):
            audio[f"----:com.apple.iTunes:{name}"] = [value.encode("utf-8")]
    else:
        audio = File(path)
        if audio.tags is None:
            audio.add_tags()
        for frame in (
            TIT2(encoding=3, text=["Song"]), TPE1(encoding=3, text=["Artist"]),
            TALB(encoding=3, text=["Record"]), TPE2(encoding=3, text=["Album Artist"]),
            TDRC(encoding=3, text=["2020-06-01"]), TDOR(encoding=3, text=["2003-01-01"]),
            TRCK(encoding=3, text=["3/12"]), TPOS(encoding=3, text=["2/2"]),
            UFID(owner="http://musicbrainz.org", data=RECORDING.encode("ascii")),
            TXXX(encoding=3, desc="MusicBrainz Album Id", text=[RELEASE]),
            TXXX(encoding=3, desc="MusicBrainz Release Group Id", text=[GROUP]),
            TXXX(encoding=3, desc="MusicBrainz Release Track Id", text=[TRACK]),
        ):
            audio.tags.add(frame)
    audio.save()


@pytest.mark.parametrize("extension", ["flac", "wav", "dsf", "m4a", "mp3"])
def test_real_container_keeps_distinct_musicbrainz_entities(tmp_path, extension):
    """实际文件中四种 MBID 和两种年份必须读入各自字段，不混用录音与发行曲目。"""
    path = tmp_path / f"03.{extension}"
    if extension == "wav":
        _write_wave(path)
    elif extension == "dsf":
        _write_dsf(path)
    else:
        shutil.copyfile(Path(__file__).parent / f"fixtures/audio/silence.{extension}", path)
    _tag_audio(path)
    before = path.read_bytes()

    meta = AudioMetadataHelper.read_tags(path)

    assert meta.media_id == RECORDING
    for key, expected in IDENTITIES.items():
        assert getattr(meta, key) == expected
        assert meta.field_sources[key] == "tag"
    assert (meta.year, meta.release_year, meta.original_year) == (2020, 2020, 2003)
    assert (meta.track_number, meta.total_tracks, meta.disc_number, meta.total_discs) == (3, 12, 2, 2)
    assert meta.title == "Song"
    assert meta.field_sources["title"] == "tag"
    assert path.read_bytes() == before


def test_release_only_tag_never_becomes_recording_identity(tmp_path):
    """只有发行 ID 时保留补充身份，不能凭空创建 Recording 主身份。"""
    path = tmp_path / "01.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
    audio = FLAC(path)
    audio["musicbrainz_albumid"] = [RELEASE]
    audio["musicbrainz_releasetrackid"] = ["invalid-id"]
    audio.save()

    meta = AudioMetadataHelper.read_tags(path)

    assert meta.musicbrainz_release_id == RELEASE
    assert meta.musicbrainz_release_track_id is None
    assert meta.media_source is None
    assert meta.media_id is None


def test_audio_evidence_roundtrip_keeps_ids_and_independent_sources():
    """领域、API、下载历史和旧缓存读写不会丢弃补充身份或共享可变来源字典。"""
    meta = MetaMusic(title="Song", artists=["Artist"], album="Record", media_source="musicbrainz",
                     media_id=RECORDING, original_year=2003, release_year=2020, total_discs=2,
                     field_sources={"title": "tag"}, **IDENTITIES)
    info = MusicInfo.from_meta(meta)
    note = DownloadChain._build_download_note("Manual", info, meta)
    decoded = json.loads(json.dumps(note))
    restored_meta = MetaMusic.from_dict(MusicMetaSchema.model_validate(decoded["music"]["meta"]).model_dump())
    restored_info = MusicInfo.from_dict(MusicInfoSchema.model_validate(decoded["music"]["media"]).model_dump())

    for value in (restored_meta, restored_info, pickle.loads(pickle.dumps(meta)), pickle.loads(pickle.dumps(info))):
        assert value.media_id == RECORDING
        assert value.original_year == 2003
        assert value.release_year == 2020
        assert value.total_discs == 2
        for key, expected in IDENTITIES.items():
            assert getattr(value, key) == expected
    meta.field_sources["title"] = "manual"
    assert info.field_sources["title"] == restored_meta.field_sources["title"] == "tag"


def test_legacy_meta_pickle_defaults_new_evidence_fields():
    """旧版解析对象没有新字段时仍可还原、套用路径和序列化。"""
    original = MetaMusic(title="Song", artists=["Artist"])
    state = dict(original.__dict__)
    for key in (*IDENTITIES, "original_year", "release_year", "field_sources"):
        state.pop(key)
    restored = MetaMusic.__new__(MetaMusic)
    restored.__setstate__(state)

    restored.apply_path_context("/music/Artist - Record (2003)/01 - Song.flac")

    assert restored.to_dict()["musicbrainz_release_id"] is None
    assert restored.field_sources["album"] == "directory"
    assert restored.title == "Song"


def test_tag_title_equal_to_filename_is_not_reparsed(tmp_path):
    """标题恰好等于文件名仍是实际标签，不能错误当作文件名兜底而清洗掉内容。"""
    path = tmp_path / "03 - Song.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
    audio = FLAC(path)
    audio["title"] = [path.stem]
    audio.save()

    meta = AudioMetadataHelper.read(path)

    assert meta.title == "03 - Song"
    assert meta.track_number == 3
    assert meta.field_sources["title"] == "tag"
    assert meta.field_sources["track_number"] == "filename"
    assert meta.field_sources["duration"] == "stream"


def _release_payload() -> dict:
    """构造同一录音在具体再版及第二碟中的 MusicBrainz 响应。"""
    return {
        "id": RELEASE, "title": "Record", "date": "2020-06-01",
        "artist-credit": [{"artist": {"id": "artist", "name": "Artist"}}],
        "release-group": {"id": GROUP, "primary-type": "Album", "first-release-date": "2003-01-01"},
        "media": [{"position": 1, "tracks": []}, {"position": 2, "track-count": 1, "tracks": [{
            "id": TRACK, "position": 1, "title": "Song", "length": 3000,
            "recording": {"id": RECORDING, "title": "Song", "first-release-date": "2003-01-01"},
        }]}],
    }


def test_selected_release_preserves_original_year_and_track_namespace():
    """具体再版日期不能被录音首发日期覆盖，发行曲目 ID 必须独立保存。"""
    album = MusicBrainzModule._release_to_album(_release_payload())
    track = album.tracks[0]

    assert album.media_id == GROUP
    assert album.musicbrainz_release_id == RELEASE
    assert track.media_id == RECORDING
    assert track.musicbrainz_release_track_id == TRACK
    assert track.musicbrainz_release_group_id == GROUP
    assert track.musicbrainz_release_id == RELEASE
    assert (track.year, track.release_year, track.original_year) == (2020, 2020, 2003)
    assert track.release_date == "2020-06-01"
    assert track.total_discs == 2
    assert track.field_sources["musicbrainz_release_id"] == "remote"
    card = MusicAlbumInfo.from_dict(album.to_dict()).to_music_info()
    assert card.musicbrainz_release_id == RELEASE
    assert card.release_year == 2020
    assert card.original_year == 2003


def test_release_without_group_does_not_forge_album_group_id():
    """残缺响应缺少发行组时不能把发行 ID 填进发行组主身份。"""
    payload = _release_payload()
    payload.pop("release-group")

    album = MusicBrainzModule._release_to_album(payload)

    assert album.media_id is None
    assert album.musicbrainz_release_group_id is None
    assert album.musicbrainz_release_id == RELEASE
    assert album.tracks[0].media_id == RECORDING


def test_group_without_selected_release_has_no_release_year():
    """发行组的首发年份不等于已经确认当前文件的发行年份。"""
    album = MusicBrainzModule._release_group_to_album({
        "id": GROUP, "title": "Record", "first-release-date": "2003-01-01",
    })

    card = album.to_music_info()

    assert card.original_year == 2003
    assert card.release_year is None
    assert card.musicbrainz_release_id is None


def test_album_meta_retains_entity_type_and_cannot_write_recording_tag():
    """专辑上下文往返 MetaMusic 后仍是专辑，不能把组 ID 写进录音标签。"""
    album = MusicAlbumInfo(media_source="musicbrainz", media_id=GROUP, title="Record")

    meta = MetaMusic.from_music_info(album.to_music_info())
    restored = MetaMusic.from_dict(meta.to_dict())
    info = MusicInfo.from_meta(restored)

    assert restored.music_type == info.music_type == "album"
    assert restored.media_id == info.media_id == GROUP
    assert AudioMetadataHelper._musicbrainz_recording_id(restored) is None


@pytest.mark.parametrize("source", [None, "musicbrainz"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_bound_album_meta_routes_as_album_with_or_without_source_override(source, asynchronous, monkeypatch):
    """指定或继承数据源时，已绑定专辑身份都不能回到默认 Recording 查询。"""
    meta = MetaMusic.from_music_info(MusicAlbumInfo(
        media_source="musicbrainz", media_id=GROUP, title="Record",
    ).to_music_info())

    chain = MediaChain()
    operation = AsyncMock(return_value=None) if asynchronous else Mock(return_value=None)
    name = "async_recognize_music_from_source" if asynchronous else "recognize_music_from_source"
    monkeypatch.setattr(chain, name, operation)
    if asynchronous:
        asyncio.run(chain._async_run_native_media_recognize({"meta": meta, "media_source": source}, cache=True))
    else:
        chain._run_native_media_recognize({"meta": meta, "media_source": source}, cache=True)

    assert operation.call_args.kwargs["music_type"] == "album"
    assert operation.call_args.kwargs["meta"].media_id == GROUP


def test_new_identity_fields_preserve_legacy_positional_constructors():
    """补充事实只能新增关键字参数，插件既有位置参数不能被悄悄错位。"""
    track = MusicInfo("musicbrainz", RECORDING, "recording", "Song", ["Artist"], [],
                      "Record", "Artist", GROUP, "Album", [], 2003)
    album = MusicAlbumInfo("musicbrainz", GROUP, "Record", ["Artist"])

    assert (track.title, track.album_type, track.year) == ("Song", "Album", 2003)
    assert album.title == "Record"
    assert album.artists == ["Artist"]


def test_explicit_release_selection_replaces_stale_local_ids(tmp_path):
    """手选发行不能留下另一个发行的标签 ID，实际音频参数仍来自当前文件。"""
    path = tmp_path / "03.flac"
    item = make_fileitem(str(path))
    local = MetaMusic(title="Old Song", album="Old Record", musicbrainz_release_id=GROUP,
                      musicbrainz_release_group_id=RELEASE, musicbrainz_release_track_id=RECORDING,
                      sample_rate=96000, field_sources={"sample_rate": "stream"})
    selected = MusicInfo(media_source="musicbrainz", media_id=RECORDING, title="Song",
                         artists=["Artist"], album="Record", release_year=2020, **IDENTITIES)

    meta, info = TransferChain()._selected_music_task_context(
        item, path, local, {str(path.resolve()): selected}, None,
    )

    for key, expected in IDENTITIES.items():
        assert getattr(meta, key) == getattr(info, key) == expected
        assert meta.field_sources[key] == "manual"
    assert meta.music_type == "recording"
    assert meta.sample_rate == info.sample_rate == 96000
    assert meta.field_sources["sample_rate"] == "stream"
    assert local.musicbrainz_release_id == GROUP
